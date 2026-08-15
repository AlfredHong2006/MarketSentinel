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


def test_daily_aggregate_includes_probability_shares_disagreement_and_trend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 14, 20, tzinfo=UTC)
    monkeypatch.setattr(aggregation_module, "utc_now", lambda: now)
    first = ScoredArticle(
        **make_article("Acme first", datetime(2026, 8, 14, 12, tzinfo=UTC)).model_dump(),
        label="positive",
        positive=0.9,
        negative=0.05,
        neutral=0.05,
        sentiment_score=0.85,
        model_name="test",
        scored_at=now,
    )
    second = ScoredArticle(
        **make_article("Acme second", datetime(2026, 8, 14, 12, tzinfo=UTC)).model_dump(),
        label="negative",
        positive=0.05,
        negative=0.9,
        neutral=0.05,
        sentiment_score=-0.85,
        model_name="test",
        scored_at=now,
    )

    result = aggregate_daily_sentiment("ACME", [first, second])

    assert result[0].score == pytest.approx(0)
    assert result[0].positive_share == pytest.approx(0.475)
    assert result[0].negative_share == pytest.approx(0.475)
    assert result[0].weighted_disagreement == pytest.approx(0.85)
    assert result[0].trend_3 == pytest.approx(0)
    assert result[0].aggregate_weight > 0
