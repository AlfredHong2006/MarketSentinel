"""Shared deterministic test factories."""

import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from marketsentinel.domain import Article, Constituent, PriceHistory, PricePoint
from marketsentinel.normalization import article_fingerprint


@pytest.fixture
def writable_tmp_path():
    """Use a repository-local temp path in restricted Windows sandboxes."""

    path = Path("data/test-runtime") / uuid4().hex
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_constituent() -> Constituent:
    return Constituent(
        symbol="ACME",
        yahoo_symbol="ACME",
        name="Acme Corporation",
        market="S&P 500",
    )


def make_article(
    title: str = "Acme Corporation shares rise after earnings",
    published_at: datetime | None = None,
    url: str = "https://example.com/story",
    source: str = "Test Wire",
    provider_article_id: str | None = None,
) -> Article:
    timestamp = published_at or datetime.now(UTC) - timedelta(hours=2)
    return Article(
        fingerprint=article_fingerprint(title, "ACME", source, timestamp),
        ticker="ACME",
        title=title,
        url=url,
        source=source,
        provider_article_id=provider_article_id,
        published_at=timestamp,
        fetched_at=datetime.now(UTC),
        provider="test-provider",
        relevance_score=0.9,
    )


def make_price_history(days: int = 320) -> PriceHistory:
    dates = np.busday_offset(date(2024, 1, 2), np.arange(days), roll="forward")
    steps = 0.12 + 0.9 * np.sin(np.arange(days) / 5.0)
    closes = 100 + np.cumsum(steps)
    points = [
        PricePoint(
            date=date.fromisoformat(str(day)),
            close=float(close),
            volume=float(1_000_000 + (index % 17) * 20_000),
        )
        for index, (day, close) in enumerate(zip(dates, closes, strict=True))
    ]
    return PriceHistory(
        symbol="ACME",
        points=points,
        fetched_at=datetime.now(UTC),
    )
