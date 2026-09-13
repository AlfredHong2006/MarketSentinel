"""SQLite repository for articles, model scores, and daily aggregates."""

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.domain import Article, ArticleAnalysis, DailySentiment, ScoredArticle
from marketsentinel.normalization import normalize_text, normalize_url
from marketsentinel.timeutils import ensure_utc

_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS articles (
    fingerprint TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL,
    source TEXT NOT NULL,
    provider_article_id TEXT,
    published_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    relevance_score REAL NOT NULL CHECK (relevance_score BETWEEN 0 AND 1),
    snippet TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_articles_ticker_published
    ON articles (ticker, published_at DESC);

CREATE TABLE IF NOT EXISTS sentiments (
    article_fingerprint TEXT PRIMARY KEY REFERENCES articles(fingerprint) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (label IN ('positive', 'negative', 'neutral')),
    positive REAL NOT NULL,
    negative REAL NOT NULL,
    neutral REAL NOT NULL,
    sentiment_score REAL NOT NULL,
    model_name TEXT NOT NULL,
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_sentiment (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    score REAL NOT NULL,
    moving_average_7d REAL NOT NULL,
    trend_3 REAL NOT NULL DEFAULT 0,
    article_count INTEGER NOT NULL,
    positive_share REAL NOT NULL DEFAULT 0,
    negative_share REAL NOT NULL DEFAULT 0,
    weighted_disagreement REAL NOT NULL DEFAULT 0,
    aggregate_weight REAL NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_sentiment_ticker_date
    ON daily_sentiment (ticker, date DESC);

CREATE TABLE IF NOT EXISTS article_intelligence_analyses (
    article_fingerprint TEXT NOT NULL REFERENCES articles(fingerprint) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    cache_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (article_fingerprint, model_version, cache_version, schema_version)
);

CREATE INDEX IF NOT EXISTS idx_article_intelligence_analyses_lookup
    ON article_intelligence_analyses (article_fingerprint, model_version, cache_version, schema_version);

-- Version 5: the coverage ledger. Operational state only -- no materiality verdict, group, rank,
-- or risk score is ever stored here; those stay recomputed from stored analyses.
CREATE TABLE IF NOT EXISTS company_coverage (
    ticker TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    ledger_started_at TEXT NOT NULL,
    live_window_days INTEGER NOT NULL CHECK (live_window_days > 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingestion_watermarks (
    ticker TEXT NOT NULL,
    provider TEXT NOT NULL,
    ingested_through TEXT,
    last_window_start TEXT,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    last_status TEXT NOT NULL CHECK (last_status IN ('ok', 'partial', 'empty', 'failed')),
    last_message TEXT,
    last_articles INTEGER NOT NULL DEFAULT 0,
    last_request_limited INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, provider)
);

CREATE TABLE IF NOT EXISTS article_analysis_jobs (
    article_fingerprint TEXT NOT NULL REFERENCES articles(fingerprint) ON DELETE CASCADE,
    analysis_contract TEXT NOT NULL,
    ticker TEXT NOT NULL,
    published_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'leased', 'retry_wait', 'analyzed', 'skipped', 'failed', 'baseline')
    ),
    reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_failure_category TEXT,
    last_failure_at TEXT,
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    analysis_evidence_fingerprint TEXT,
    analysis_created_at TEXT,
    evidence_checked_at TEXT,
    evidence_current INTEGER CHECK (evidence_current IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT,
    PRIMARY KEY (article_fingerprint, analysis_contract)
);

CREATE INDEX IF NOT EXISTS idx_article_analysis_jobs_ticker_state
    ON article_analysis_jobs (ticker, analysis_contract, state);

PRAGMA user_version = 5;
"""

# Job states. Terminal states are never left by an ordinary transition; the single exception is
# that a stored current-contract analysis always completes a job as ``analyzed``, because the
# stored analysis is the ground truth the ledger describes.
JOB_ACTIVE_STATES = ("pending", "leased", "retry_wait")
JOB_TERMINAL_STATES = ("analyzed", "skipped", "failed", "baseline")

_DAILY_SENTIMENT_MIGRATIONS = {
    "trend_3": "REAL NOT NULL DEFAULT 0",
    "positive_share": "REAL NOT NULL DEFAULT 0",
    "negative_share": "REAL NOT NULL DEFAULT 0",
    "weighted_disagreement": "REAL NOT NULL DEFAULT 0",
    "aggregate_weight": "REAL NOT NULL DEFAULT 0",
}

_ARTICLE_MIGRATIONS = {
    "provider_article_id": "TEXT",
    "snippet": "TEXT",
}

LOGGER = logging.getLogger(__name__)


class SQLiteRepository:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)
            existing_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(daily_sentiment)")
            }
            for name, definition in _DAILY_SENTIMENT_MIGRATIONS.items():
                if name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE daily_sentiment ADD COLUMN {name} {definition}"
                    )
            article_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(articles)")
            }
            for name, definition in _ARTICLE_MIGRATIONS.items():
                if name not in article_columns:
                    connection.execute(f"ALTER TABLE articles ADD COLUMN {name} {definition}")

    def upsert_articles(self, articles: Iterable[Article]) -> None:
        rows = [
            (
                item.fingerprint,
                item.ticker,
                item.title,
                normalize_text(item.title),
                item.url,
                normalize_url(item.url),
                item.source,
                item.provider_article_id,
                item.published_at.isoformat(),
                item.fetched_at.isoformat(),
                item.provider,
                item.relevance_score,
                item.snippet,
                int(item.is_demo),
            )
            for item in articles
        ]
        if not rows:
            return
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT INTO articles (
                    fingerprint, ticker, title, normalized_title, url, normalized_url,
                    source, provider_article_id, published_at, fetched_at, provider, relevance_score, snippet, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    normalized_url = excluded.normalized_url,
                    source = excluded.source,
                    provider_article_id = excluded.provider_article_id,
                    published_at = MAX(articles.published_at, excluded.published_at),
                    fetched_at = excluded.fetched_at,
                    provider = excluded.provider,
                    relevance_score = MAX(articles.relevance_score, excluded.relevance_score),
                    snippet = COALESCE(excluded.snippet, articles.snippet),
                    is_demo = MIN(articles.is_demo, excluded.is_demo)
                """,
                rows,
            )

    def scored_fingerprints(self, fingerprints: Iterable[str]) -> set[str]:
        values = list(fingerprints)
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"SELECT article_fingerprint FROM sentiments "
                f"WHERE article_fingerprint IN ({placeholders})",
                values,
            ).fetchall()
        return {str(row["article_fingerprint"]) for row in rows}

    def article_dedupe_keys(
        self,
        ticker: str,
        since: datetime | None = None,
    ) -> list[tuple[str, str]]:
        """Return persisted canonical URLs and normalized titles for idempotent refresh filtering."""

        query = "SELECT normalized_url, normalized_title FROM articles WHERE ticker = ?"
        parameters: list[object] = [ticker]
        if since is not None:
            query += " AND published_at >= ?"
            parameters.append(since.isoformat())
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, parameters).fetchall()
        return [(str(row["normalized_url"]), str(row["normalized_title"])) for row in rows]

    def list_articles(self, ticker: str, since: datetime | None = None) -> list[Article]:
        query = "SELECT * FROM articles WHERE ticker = ?"
        parameters: list[object] = [ticker]
        if since is not None:
            query += " AND published_at >= ?"
            parameters.append(since.isoformat())
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_row_to_article(row) for row in rows]

    def get_article(self, fingerprint: str) -> Article | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return _row_to_article(row) if row is not None else None

    def list_evidence_articles(
        self,
        ticker: str,
        exclude_fingerprint: str,
        limit: int = 5,
    ) -> list[Article]:
        """Return a deliberately small, same-company set of stored comparison records."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT * FROM articles
                WHERE ticker = ? AND fingerprint != ? AND is_demo = 0
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (ticker, exclude_fingerprint, limit),
            ).fetchall()
        return [_row_to_article(row) for row in rows]

    def get_article_analysis(
        self,
        article_id: str,
        model_version: str,
        cache_version: str,
        schema_version: str,
    ) -> ArticleAnalysis | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT analysis_json FROM article_intelligence_analyses
                WHERE article_fingerprint = ? AND model_version = ?
                  AND cache_version = ? AND schema_version = ?
                """,
                (article_id, model_version, cache_version, schema_version),
            ).fetchone()
        if row is None:
            return None
        try:
            return ArticleAnalysis.model_validate_json(row["analysis_json"])
        except ValidationError:
            LOGGER.warning(
                "Ignoring incompatible cached article analysis for article_id=%s",
                article_id,
            )
            return None

    def store_article_analysis(self, analysis: ArticleAnalysis, cache_version: str) -> None:
        """Append a versioned successful result; never overwrite a prior version's payload."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO article_intelligence_analyses (
                    article_fingerprint, model_version, cache_version, schema_version,
                    analysis_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_fingerprint, model_version, cache_version, schema_version)
                DO NOTHING
                """,
                (
                    analysis.article_id,
                    analysis.model_version,
                    cache_version,
                    analysis.schema_version,
                    analysis.model_dump_json(),
                    analysis.analysis_created_at.isoformat(),
                ),
            )

    def list_article_analyses(
        self,
        ticker: str,
        since: datetime | None = None,
        limit: int = 100,
        compatibility: ArticleAnalysisCompatibility | None = None,
    ) -> list[ArticleAnalysis]:
        """Return the newest stored analysis version for each genuine company article."""

        filters = ["a.ticker = ?", "a.is_demo = 0"]
        parameters: list[object] = [ticker]
        if since is not None:
            filters.append("a.published_at >= ?")
            parameters.append(since.isoformat())
        where_clause = " AND ".join(filters)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"""
                SELECT analyses.article_fingerprint, analyses.analysis_json
                FROM article_intelligence_analyses AS analyses
                JOIN articles AS a
                  ON a.fingerprint = analyses.article_fingerprint
                WHERE {where_clause}
                ORDER BY a.published_at DESC, analyses.created_at DESC, analyses.rowid DESC
                """,
                parameters,
            ).fetchall()
        results: list[ArticleAnalysis] = []
        seen_articles: set[str] = set()
        for row in rows:
            article_id = str(row["article_fingerprint"])
            if article_id in seen_articles:
                continue
            try:
                analysis = ArticleAnalysis.model_validate_json(row["analysis_json"])
            except ValidationError:
                LOGGER.warning(
                    "Skipping incompatible stored article analysis for article_id=%s",
                    article_id,
                )
                continue
            if compatibility is not None and not compatibility.accepts_for_display(analysis):
                LOGGER.info("Skipping stale stored article analysis for article_id=%s", article_id)
                continue
            results.append(analysis)
            seen_articles.add(article_id)
            if len(results) >= limit:
                break
        return results

    def stored_article_counts(self) -> dict[str, int]:
        """Raw stored-article count per ticker, demo rows excluded, ordered by ticker.

        Feeds the capabilities endpoint's coverage map: a plain fact about the database so a
        client can say which companies have stored coverage. Deliberately not an analysed count,
        a browsable-row count, or any kind of quality metric.
        """

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT ticker, COUNT(*) FROM articles WHERE is_demo = 0 "
                "GROUP BY ticker ORDER BY ticker"
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def upsert_sentiments(self, articles: Iterable[ScoredArticle]) -> None:
        rows = [
            (
                item.fingerprint,
                item.label,
                item.positive,
                item.negative,
                item.neutral,
                item.sentiment_score,
                item.model_name,
                item.scored_at.isoformat(),
            )
            for item in articles
        ]
        if not rows:
            return
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT INTO sentiments (
                    article_fingerprint, label, positive, negative, neutral,
                    sentiment_score, model_name, scored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_fingerprint) DO UPDATE SET
                    label = excluded.label,
                    positive = excluded.positive,
                    negative = excluded.negative,
                    neutral = excluded.neutral,
                    sentiment_score = excluded.sentiment_score,
                    model_name = excluded.model_name,
                    scored_at = excluded.scored_at
                """,
                rows,
            )

    def list_scored_articles(
        self,
        ticker: str,
        since: datetime | None = None,
        limit: int | None = 500,
    ) -> list[ScoredArticle]:
        """Return stored scored articles, newest first.

        ``limit=None`` returns every row matching the filters. A caller uses it when its window
        is already the bound -- a row cap on top of a bounded window silently drops real stored
        articles and makes a truncated page read as the whole corpus.
        """

        query = """
            SELECT a.*, s.label, s.positive, s.negative, s.neutral,
                   s.sentiment_score, s.model_name, s.scored_at
            FROM articles AS a
            JOIN sentiments AS s ON s.article_fingerprint = a.fingerprint
            WHERE a.ticker = ?
        """
        parameters: list[object] = [ticker]
        if since is not None:
            query += " AND a.published_at >= ?"
            parameters.append(since.isoformat())
        query += " ORDER BY a.published_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_row_to_scored_article(row) for row in rows]

    def upsert_daily_sentiment(self, values: Iterable[DailySentiment]) -> None:
        rows = [
            (
                item.ticker,
                item.date.isoformat(),
                item.score,
                item.moving_average_7d,
                item.trend_3,
                item.article_count,
                item.positive_share,
                item.negative_share,
                item.weighted_disagreement,
                item.aggregate_weight,
                item.computed_at.isoformat(),
            )
            for item in values
        ]
        if not rows:
            return
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT INTO daily_sentiment (
                    ticker, date, score, moving_average_7d, trend_3, article_count,
                    positive_share, negative_share, weighted_disagreement, aggregate_weight, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET
                    score = excluded.score,
                    moving_average_7d = excluded.moving_average_7d,
                    trend_3 = excluded.trend_3,
                    article_count = excluded.article_count,
                    positive_share = excluded.positive_share,
                    negative_share = excluded.negative_share,
                    weighted_disagreement = excluded.weighted_disagreement,
                    aggregate_weight = excluded.aggregate_weight,
                    computed_at = excluded.computed_at
                """,
                rows,
            )

    def delete_daily_sentiment(self, ticker: str, since: date) -> None:
        """Remove derived values before recomputing a ticker's bounded historical window."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM daily_sentiment WHERE ticker = ? AND date >= ?",
                (ticker, since.isoformat()),
            )

    def list_daily_sentiment(self, ticker: str, limit: int = 365) -> list[DailySentiment]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT ticker, date, score, moving_average_7d, trend_3, article_count,
                       positive_share, negative_share, weighted_disagreement, aggregate_weight, computed_at
                FROM daily_sentiment
                WHERE ticker = ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (ticker, limit),
            ).fetchall()
        values = [
            DailySentiment(
                ticker=row["ticker"],
                date=date.fromisoformat(row["date"]),
                score=row["score"],
                moving_average_7d=row["moving_average_7d"],
                trend_3=row["trend_3"],
                article_count=row["article_count"],
                positive_share=row["positive_share"],
                negative_share=row["negative_share"],
                weighted_disagreement=row["weighted_disagreement"],
                aggregate_weight=row["aggregate_weight"],
                computed_at=datetime.fromisoformat(row["computed_at"]),
            )
            for row in rows
        ]
        return list(reversed(values))

    # -- Coverage ledger ----------------------------------------------------------------------

    def activate_company_coverage(
        self, ticker: str, *, started_at: datetime, live_window_days: int
    ) -> "CompanyCoverage":
        """Register a ticker for continuous coverage. Idempotent: a re-activation keeps the
        original ``ledger_started_at`` and live window, so the baseline boundary never moves."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO company_coverage (
                    ticker, active, ledger_started_at, live_window_days, updated_at
                ) VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET active = 1, updated_at = excluded.updated_at
                """,
                (ticker, _timestamp(started_at), live_window_days, _timestamp(started_at)),
            )
        coverage = self.get_company_coverage(ticker)
        assert coverage is not None
        return coverage

    def get_company_coverage(self, ticker: str) -> "CompanyCoverage | None":
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM company_coverage WHERE ticker = ?", (ticker,)
            ).fetchone()
        if row is None:
            return None
        return CompanyCoverage(
            ticker=row["ticker"],
            active=bool(row["active"]),
            ledger_started_at=datetime.fromisoformat(row["ledger_started_at"]),
            live_window_days=int(row["live_window_days"]),
        )

    def get_ingestion_watermark(self, ticker: str, provider: str) -> "IngestionWatermark | None":
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM ingestion_watermarks WHERE ticker = ? AND provider = ?",
                (ticker, provider),
            ).fetchone()
        return _row_to_watermark(row) if row is not None else None

    def list_ingestion_watermarks(self, ticker: str) -> list["IngestionWatermark"]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM ingestion_watermarks WHERE ticker = ? ORDER BY provider", (ticker,)
            ).fetchall()
        return [_row_to_watermark(row) for row in rows]

    def record_ingestion_attempt(
        self,
        *,
        ticker: str,
        provider: str,
        window_start: datetime,
        attempted_at: datetime,
        status: str,
        message: str | None,
        articles: int,
        request_limited: int,
        ingested_through: datetime | None,
    ) -> None:
        """Record one provider fetch for one ticker.

        ``ingested_through`` is supplied only when the fetch completed; ``None`` leaves the
        existing watermark in place. The watermark never moves backwards.

        ``consecutive_failures`` counts consecutive *problem* fetches -- ``failed`` and
        ``partial`` alike, since neither advances the watermark -- and resets on ``ok`` or
        ``empty``. ``last_success_at`` records only completed fetches.
        """

        problem = status in ("failed", "partial")
        through = _timestamp(ingested_through) if ingested_through is not None else None
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO ingestion_watermarks (
                    ticker, provider, ingested_through, last_window_start, last_attempt_at,
                    last_success_at, last_status, last_message, last_articles,
                    last_request_limited, consecutive_failures
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, provider) DO UPDATE SET
                    ingested_through = CASE
                        WHEN excluded.ingested_through IS NULL
                            THEN ingestion_watermarks.ingested_through
                        WHEN ingestion_watermarks.ingested_through IS NULL
                            THEN excluded.ingested_through
                        ELSE MAX(ingestion_watermarks.ingested_through, excluded.ingested_through)
                    END,
                    last_window_start = excluded.last_window_start,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = COALESCE(
                        excluded.last_success_at, ingestion_watermarks.last_success_at
                    ),
                    last_status = excluded.last_status,
                    last_message = excluded.last_message,
                    last_articles = excluded.last_articles,
                    last_request_limited = excluded.last_request_limited,
                    consecutive_failures = CASE
                        WHEN excluded.last_status IN ('failed', 'partial')
                            THEN ingestion_watermarks.consecutive_failures + 1
                        ELSE 0
                    END
                """,
                (
                    ticker,
                    provider,
                    through,
                    _timestamp(window_start),
                    _timestamp(attempted_at),
                    _timestamp(attempted_at) if ingested_through is not None else None,
                    status,
                    message,
                    articles,
                    request_limited,
                    1 if problem else 0,
                ),
            )

    def insert_analysis_jobs(self, jobs: Sequence["NewAnalysisJob"]) -> int:
        """Create job rows; an existing (article, contract) row is never replaced.

        Returns how many rows were actually inserted, so a repeated reconcile reports zero.
        """

        if not jobs:
            return 0
        rows = [
            (
                job.article_fingerprint,
                job.analysis_contract,
                job.ticker,
                _timestamp(job.published_at),
                job.state,
                job.reason,
                job.analysis_evidence_fingerprint,
                _timestamp(job.analysis_created_at) if job.analysis_created_at else None,
                _timestamp(job.created_at),
                _timestamp(job.created_at),
                _timestamp(job.created_at) if job.state in JOB_TERMINAL_STATES else None,
            )
            for job in jobs
        ]
        with closing(self._connect()) as connection, connection:
            before = connection.total_changes
            connection.executemany(
                """
                INSERT INTO article_analysis_jobs (
                    article_fingerprint, analysis_contract, ticker, published_at, state, reason,
                    analysis_evidence_fingerprint, analysis_created_at, created_at, updated_at,
                    terminal_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_fingerprint, analysis_contract) DO NOTHING
                """,
                rows,
            )
            return connection.total_changes - before

    def articles_without_analysis_job(self, ticker: str, analysis_contract: str) -> list[Article]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT a.* FROM articles AS a
                LEFT JOIN article_analysis_jobs AS j
                  ON j.article_fingerprint = a.fingerprint AND j.analysis_contract = ?
                WHERE a.ticker = ? AND j.article_fingerprint IS NULL
                ORDER BY a.published_at DESC, a.fingerprint
                """,
                (analysis_contract, ticker),
            ).fetchall()
        return [_row_to_article(row) for row in rows]

    def get_analysis_job(
        self, article_fingerprint: str, analysis_contract: str
    ) -> "AnalysisJob | None":
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM article_analysis_jobs "
                "WHERE article_fingerprint = ? AND analysis_contract = ?",
                (article_fingerprint, analysis_contract),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def list_analysis_jobs(
        self,
        ticker: str,
        analysis_contract: str,
        states: Sequence[str] | None = None,
    ) -> list["AnalysisJob"]:
        query = "SELECT * FROM article_analysis_jobs WHERE ticker = ? AND analysis_contract = ?"
        parameters: list[object] = [ticker, analysis_contract]
        if states:
            query += f" AND state IN ({','.join('?' for _ in states)})"
            parameters.extend(states)
        query += " ORDER BY published_at DESC, article_fingerprint"
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_due_analysis_jobs(
        self, ticker: str, analysis_contract: str, now: datetime
    ) -> list["AnalysisJob"]:
        """Jobs an automatic pass may claim now: pending, due retries, and expired leases."""

        stamp = _timestamp(now)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT * FROM article_analysis_jobs
                WHERE ticker = ? AND analysis_contract = ?
                  AND (
                    state = 'pending'
                    OR (state = 'retry_wait' AND next_attempt_at <= ?)
                    OR (state = 'leased' AND lease_expires_at <= ?)
                  )
                ORDER BY published_at DESC, article_fingerprint
                """,
                (ticker, analysis_contract, stamp, stamp),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def analysis_job_summary(self, ticker: str, analysis_contract: str) -> "AnalysisJobSummary":
        with closing(self._connect()) as connection, connection:
            state_rows = connection.execute(
                "SELECT state, COUNT(*) FROM article_analysis_jobs "
                "WHERE ticker = ? AND analysis_contract = ? GROUP BY state",
                (ticker, analysis_contract),
            ).fetchall()
            totals = connection.execute(
                """
                SELECT COALESCE(SUM(attempts), 0), COALESCE(SUM(failure_count), 0),
                       COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(CASE WHEN evidence_current = 0 THEN 1 ELSE 0 END), 0)
                FROM article_analysis_jobs WHERE ticker = ? AND analysis_contract = ?
                """,
                (ticker, analysis_contract),
            ).fetchone()
            untracked = connection.execute(
                """
                SELECT COUNT(*) FROM articles AS a
                LEFT JOIN article_analysis_jobs AS j
                  ON j.article_fingerprint = a.fingerprint AND j.analysis_contract = ?
                WHERE a.ticker = ? AND j.article_fingerprint IS NULL
                """,
                (analysis_contract, ticker),
            ).fetchone()
        return AnalysisJobSummary(
            states={str(row[0]): int(row[1]) for row in state_rows},
            attempts=int(totals[0]),
            failures=int(totals[1]),
            input_tokens=int(totals[2]),
            output_tokens=int(totals[3]),
            evidence_changed=int(totals[4]),
            articles_without_job=int(untracked[0]),
        )

    def contract_analyses(
        self, ticker: str, compatibility: ArticleAnalysisCompatibility
    ) -> dict[str, ArticleAnalysis]:
        """Newest stored analysis per article that completes a job under ``compatibility``."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT analyses.article_fingerprint, analyses.analysis_json
                FROM article_intelligence_analyses AS analyses
                JOIN articles AS a ON a.fingerprint = analyses.article_fingerprint
                WHERE a.ticker = ? AND analyses.model_version = ?
                  AND analyses.schema_version = ?
                ORDER BY analyses.created_at DESC, analyses.rowid DESC
                """,
                (ticker, compatibility.model_version, compatibility.schema_version),
            ).fetchall()
        return _newest_contract_analyses(rows, compatibility)

    def latest_contract_analysis(
        self, article_fingerprint: str, compatibility: ArticleAnalysisCompatibility
    ) -> ArticleAnalysis | None:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT article_fingerprint, analysis_json FROM article_intelligence_analyses
                WHERE article_fingerprint = ? AND model_version = ? AND schema_version = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (article_fingerprint, compatibility.model_version, compatibility.schema_version),
            ).fetchall()
        return _newest_contract_analyses(rows, compatibility).get(article_fingerprint)

    def claim_analysis_job(
        self,
        article_fingerprint: str,
        analysis_contract: str,
        *,
        owner: str,
        now: datetime,
        lease_expires_at: datetime,
        allow_terminal_retry: bool = False,
    ) -> "AnalysisJob | None":
        """Atomically lease one job, or return ``None`` when it is not claimable.

        A single compare-and-set UPDATE, so two processes can never both hold a job. Reclaiming
        an expired lease counts the abandoned attempt, because a paid call may have happened.
        ``allow_terminal_retry`` is only for an explicit per-article operator request.
        """

        claimable = (
            "state = 'pending' OR (state = 'retry_wait' AND next_attempt_at <= :now) "
            "OR (state = 'leased' AND lease_expires_at <= :now)"
        )
        if allow_terminal_retry:
            claimable += " OR state IN ('retry_wait', 'failed', 'skipped', 'baseline')"
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"""
                UPDATE article_analysis_jobs SET
                    attempts = attempts + CASE WHEN state = 'leased' THEN 1 ELSE 0 END,
                    failure_count = failure_count + CASE WHEN state = 'leased' THEN 1 ELSE 0 END,
                    last_failure_category = CASE
                        WHEN state = 'leased' THEN 'lease_expired' ELSE last_failure_category
                    END,
                    last_failure_at = CASE
                        WHEN state = 'leased' THEN :now ELSE last_failure_at
                    END,
                    state = 'leased',
                    lease_owner = :owner,
                    lease_expires_at = :lease,
                    next_attempt_at = NULL,
                    terminal_at = NULL,
                    updated_at = :now
                WHERE article_fingerprint = :fingerprint AND analysis_contract = :contract
                  AND ({claimable})
                """,
                {
                    "now": _timestamp(now),
                    "owner": owner,
                    "lease": _timestamp(lease_expires_at),
                    "fingerprint": article_fingerprint,
                    "contract": analysis_contract,
                },
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM article_analysis_jobs "
                "WHERE article_fingerprint = ? AND analysis_contract = ?",
                (article_fingerprint, analysis_contract),
            ).fetchone()
        return _row_to_job(row)

    def finish_analysis_job(
        self,
        article_fingerprint: str,
        analysis_contract: str,
        *,
        owner: str,
        now: datetime,
        state: str,
        reason: str | None,
        paid_attempt: bool,
        input_tokens: int,
        output_tokens: int,
        failure_category: str | None = None,
        next_attempt_at: datetime | None = None,
        analysis: ArticleAnalysis | None = None,
    ) -> bool:
        """Record one attempt's outcome and release the lease.

        Attempts, tokens, and failures are added unconditionally, because the spend happened
        whether or not this process still holds the lease. The state transition itself is a
        compare-and-set on the lease owner, so a process whose lease was reclaimed cannot
        overwrite the new holder's state. Returns whether the transition applied.
        """

        stamp = _timestamp(now)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE article_analysis_jobs SET
                    attempts = attempts + ?,
                    input_tokens = input_tokens + ?,
                    output_tokens = output_tokens + ?,
                    failure_count = failure_count + ?,
                    last_failure_category = COALESCE(?, last_failure_category),
                    last_failure_at = CASE WHEN ? IS NULL THEN last_failure_at ELSE ? END,
                    updated_at = ?
                WHERE article_fingerprint = ? AND analysis_contract = ?
                """,
                (
                    1 if paid_attempt else 0,
                    input_tokens,
                    output_tokens,
                    1 if failure_category else 0,
                    failure_category,
                    failure_category,
                    stamp,
                    stamp,
                    article_fingerprint,
                    analysis_contract,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE article_analysis_jobs SET
                    state = ?,
                    reason = ?,
                    next_attempt_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    analysis_evidence_fingerprint = COALESCE(?, analysis_evidence_fingerprint),
                    analysis_created_at = COALESCE(?, analysis_created_at),
                    evidence_checked_at = CASE
                        WHEN ? IS NULL THEN evidence_checked_at ELSE NULL
                    END,
                    evidence_current = CASE WHEN ? IS NULL THEN evidence_current ELSE NULL END,
                    terminal_at = ?,
                    updated_at = ?
                WHERE article_fingerprint = ? AND analysis_contract = ?
                  AND state = 'leased' AND lease_owner = ?
                """,
                (
                    state,
                    reason,
                    _timestamp(next_attempt_at) if next_attempt_at is not None else None,
                    analysis.evidence_fingerprint if analysis else None,
                    _timestamp(analysis.analysis_created_at) if analysis else None,
                    analysis.evidence_fingerprint if analysis else None,
                    analysis.evidence_fingerprint if analysis else None,
                    stamp if state in JOB_TERMINAL_STATES else None,
                    stamp,
                    article_fingerprint,
                    analysis_contract,
                    owner,
                ),
            )
            return cursor.rowcount == 1

    def complete_analysis_job_from_existing(
        self,
        article_fingerprint: str,
        analysis_contract: str,
        *,
        analysis: ArticleAnalysis,
        now: datetime,
    ) -> bool:
        """Mark a job ``analyzed`` because a current-contract analysis is already stored.

        Costs nothing. A job actively leased by another process is left alone: that process is
        the one producing the analysis and will record its own attempt and tokens.
        """

        stamp = _timestamp(now)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE article_analysis_jobs SET
                    state = 'analyzed',
                    reason = 'preexisting',
                    next_attempt_at = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    analysis_evidence_fingerprint = ?,
                    analysis_created_at = ?,
                    evidence_checked_at = NULL,
                    evidence_current = NULL,
                    terminal_at = ?,
                    updated_at = ?
                WHERE article_fingerprint = ? AND analysis_contract = ?
                  AND state != 'analyzed'
                  AND NOT (state = 'leased' AND lease_expires_at > ?)
                """,
                (
                    analysis.evidence_fingerprint,
                    _timestamp(analysis.analysis_created_at),
                    stamp,
                    stamp,
                    article_fingerprint,
                    analysis_contract,
                    stamp,
                ),
            )
            return cursor.rowcount == 1

    def record_evidence_check(
        self,
        article_fingerprint: str,
        analysis_contract: str,
        *,
        analysis: ArticleAnalysis,
        current: bool,
        now: datetime,
    ) -> None:
        """Record whether an analysed job's newest stored analysis still has current evidence."""

        stamp = _timestamp(now)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE article_analysis_jobs SET
                    analysis_evidence_fingerprint = ?,
                    analysis_created_at = ?,
                    evidence_checked_at = ?,
                    evidence_current = ?,
                    updated_at = ?
                WHERE article_fingerprint = ? AND analysis_contract = ? AND state = 'analyzed'
                """,
                (
                    analysis.evidence_fingerprint,
                    _timestamp(analysis.analysis_created_at),
                    stamp,
                    1 if current else 0,
                    stamp,
                    article_fingerprint,
                    analysis_contract,
                ),
            )


@dataclass(frozen=True)
class CompanyCoverage:
    ticker: str
    active: bool
    ledger_started_at: datetime
    live_window_days: int


@dataclass(frozen=True)
class IngestionWatermark:
    ticker: str
    provider: str
    ingested_through: datetime | None
    last_window_start: datetime | None
    last_attempt_at: datetime
    last_success_at: datetime | None
    last_status: str
    last_message: str | None
    last_articles: int
    last_request_limited: int
    consecutive_failures: int


@dataclass(frozen=True)
class NewAnalysisJob:
    article_fingerprint: str
    analysis_contract: str
    ticker: str
    published_at: datetime
    state: str
    reason: str | None
    created_at: datetime
    analysis_evidence_fingerprint: str | None = None
    analysis_created_at: datetime | None = None


@dataclass(frozen=True)
class AnalysisJob:
    article_fingerprint: str
    analysis_contract: str
    ticker: str
    published_at: datetime
    state: str
    reason: str | None
    attempts: int
    failure_count: int
    last_failure_category: str | None
    next_attempt_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    input_tokens: int
    output_tokens: int
    analysis_evidence_fingerprint: str | None
    analysis_created_at: datetime | None
    evidence_checked_at: datetime | None
    evidence_current: bool | None
    terminal_at: datetime | None


@dataclass(frozen=True)
class AnalysisJobSummary:
    states: dict[str, int]
    attempts: int
    failures: int
    input_tokens: int
    output_tokens: int
    evidence_changed: int
    articles_without_job: int


def _timestamp(value: datetime) -> str:
    """One fixed-width UTC form, so ledger timestamps compare correctly as text in SQL."""

    return ensure_utc(value).isoformat(timespec="microseconds")


def _optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _row_to_watermark(row: sqlite3.Row) -> IngestionWatermark:
    return IngestionWatermark(
        ticker=row["ticker"],
        provider=row["provider"],
        ingested_through=_optional_datetime(row["ingested_through"]),
        last_window_start=_optional_datetime(row["last_window_start"]),
        last_attempt_at=datetime.fromisoformat(row["last_attempt_at"]),
        last_success_at=_optional_datetime(row["last_success_at"]),
        last_status=row["last_status"],
        last_message=row["last_message"],
        last_articles=int(row["last_articles"]),
        last_request_limited=int(row["last_request_limited"]),
        consecutive_failures=int(row["consecutive_failures"]),
    )


def _row_to_job(row: sqlite3.Row) -> AnalysisJob:
    evidence_current = row["evidence_current"]
    return AnalysisJob(
        article_fingerprint=row["article_fingerprint"],
        analysis_contract=row["analysis_contract"],
        ticker=row["ticker"],
        published_at=datetime.fromisoformat(row["published_at"]),
        state=row["state"],
        reason=row["reason"],
        attempts=int(row["attempts"]),
        failure_count=int(row["failure_count"]),
        last_failure_category=row["last_failure_category"],
        next_attempt_at=_optional_datetime(row["next_attempt_at"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=_optional_datetime(row["lease_expires_at"]),
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        analysis_evidence_fingerprint=row["analysis_evidence_fingerprint"],
        analysis_created_at=_optional_datetime(row["analysis_created_at"]),
        evidence_checked_at=_optional_datetime(row["evidence_checked_at"]),
        evidence_current=None if evidence_current is None else bool(evidence_current),
        terminal_at=_optional_datetime(row["terminal_at"]),
    )


def _newest_contract_analyses(
    rows: Iterable[sqlite3.Row], compatibility: ArticleAnalysisCompatibility
) -> dict[str, ArticleAnalysis]:
    newest: dict[str, ArticleAnalysis] = {}
    for row in rows:
        article_id = str(row["article_fingerprint"])
        if article_id in newest:
            continue
        try:
            analysis = ArticleAnalysis.model_validate_json(row["analysis_json"])
        except ValidationError:
            continue
        if compatibility.accepts_for_contract(analysis):
            newest[article_id] = analysis
    return newest


def _row_to_scored_article(row: sqlite3.Row) -> ScoredArticle:
    return ScoredArticle(
        fingerprint=row["fingerprint"],
        ticker=row["ticker"],
        title=row["title"],
        url=row["url"],
        source=row["source"],
        provider_article_id=row["provider_article_id"],
        published_at=datetime.fromisoformat(row["published_at"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        provider=row["provider"],
        relevance_score=row["relevance_score"],
        snippet=row["snippet"],
        is_demo=bool(row["is_demo"]),
        label=row["label"],
        positive=row["positive"],
        negative=row["negative"],
        neutral=row["neutral"],
        sentiment_score=row["sentiment_score"],
        model_name=row["model_name"],
        scored_at=datetime.fromisoformat(row["scored_at"]),
    )


def _row_to_article(row: sqlite3.Row) -> Article:
    return Article(
        fingerprint=row["fingerprint"],
        ticker=row["ticker"],
        title=row["title"],
        url=row["url"],
        source=row["source"],
        provider_article_id=row["provider_article_id"],
        published_at=datetime.fromisoformat(row["published_at"]),
        fetched_at=datetime.fromisoformat(row["fetched_at"]),
        provider=row["provider"],
        relevance_score=row["relevance_score"],
        snippet=row["snippet"],
        is_demo=bool(row["is_demo"]),
    )
