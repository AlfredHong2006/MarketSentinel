"""SQLite repository for articles, model scores, and daily aggregates."""

import sqlite3
from collections.abc import Iterable
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from marketsentinel.domain import Article, DailySentiment, ScoredArticle
from marketsentinel.normalization import normalize_text, normalize_url

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

PRAGMA user_version = 2;
"""

_DAILY_SENTIMENT_MIGRATIONS = {
    "trend_3": "REAL NOT NULL DEFAULT 0",
    "positive_share": "REAL NOT NULL DEFAULT 0",
    "negative_share": "REAL NOT NULL DEFAULT 0",
    "weighted_disagreement": "REAL NOT NULL DEFAULT 0",
    "aggregate_weight": "REAL NOT NULL DEFAULT 0",
}

_ARTICLE_MIGRATIONS = {
    "provider_article_id": "TEXT",
}


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
                    source, provider_article_id, published_at, fetched_at, provider, relevance_score, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        limit: int = 500,
    ) -> list[ScoredArticle]:
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
        query += " ORDER BY a.published_at DESC LIMIT ?"
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
        is_demo=bool(row["is_demo"]),
    )
