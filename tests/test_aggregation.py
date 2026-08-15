from datetime import UTC, datetime

import pytest
from conftest import make_article

from marketsentinel.aggregation import sentiment as aggregation_module
from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.domain import ScoredArticle


def scored(title: str, published_at: datetime, score: float) -> ScoredArticle:
    article = make_article(title=title, published_at=published_at)
    positive = max(score, 0)
    negative = max(-score, 0)
    neutral = 1 - positive - negative
    return ScoredArticle(
        **article.model_dump(),
        label="positive" if score > 0 else "negative",
        positive=positive,
        negative=negative,
        neutral=neutral,
        sentiment_score=score,
        model_name="test",
        scored_at=published_at,
    )


def test_daily_score_uses_exponential_recency_weights(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 8, 14, 20, tzinfo=UTC)
    monkeypatch.setattr(aggregation_module, "utc_now", lambda: now)
    older = scored("Acme old negative", datetime(2026, 8, 14, 4, tzinfo=UTC), -1)
    newer = scored("Acme new positive", datetime(2026, 8, 14, 16, tzinfo=UTC), 1)

    result = aggregate_daily_sentiment("ACME", [older, newer], half_life_hours=12)

    assert len(result) == 1
    assert result[0].score == pytest.approx(1 / 3)
    assert result[0].article_count == 2


def test_moving_average_uses_previous_seven_calendar_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 14, 20, tzinfo=UTC)
    monkeypatch.setattr(aggregation_module, "utc_now", lambda: now)
    articles = [
        scored("Acme first", datetime(2026, 8, 1, 12, tzinfo=UTC), -1),
        scored("Acme second", datetime(2026, 8, 8, 12, tzinfo=UTC), 1),
        scored("Acme third", datetime(2026, 8, 14, 12, tzinfo=UTC), 0.5),
    ]

    result = aggregate_daily_sentiment("ACME", articles)

    assert result[-1].moving_average_7d == pytest.approx(0.75)
