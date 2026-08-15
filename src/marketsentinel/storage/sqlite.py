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
    article_count INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_sentiment_ticker_date
    ON daily_sentiment (ticker, date DESC);

PRAGMA user_version = 1;
"""


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
                    source, published_at, fetched_at, provider, relevance_score, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    title = excluded.title,
                    url = excluded.url,
                    normalized_url = excluded.normalized_url,
                    source = excluded.source,
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
                item.article_count,
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
                    ticker, date, score, moving_average_7d, article_count, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET
                    score = excluded.score,
                    moving_average_7d = excluded.moving_average_7d,
                    article_count = excluded.article_count,
                    computed_at = excluded.computed_at
                """,
                rows,
            )

    def list_daily_sentiment(self, ticker: str, limit: int = 365) -> list[DailySentiment]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT ticker, date, score, moving_average_7d, article_count, computed_at
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
                article_count=row["article_count"],
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
