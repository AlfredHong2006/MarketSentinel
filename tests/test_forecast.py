from datetime import UTC, datetime, timedelta

from conftest import make_price_history

from marketsentinel.domain import DailySentiment
from marketsentinel.forecasting.baseline import BaselineForecaster, _feature_frame


def test_forecast_uses_chronological_matured_targets_and_available_sentiment() -> None:
    history = make_price_history()
    sentiment = [
        DailySentiment(
            ticker="ACME",
            date=point.date,
            score=(index % 5 - 2) / 4,
            moving_average_7d=0.1,
            article_count=2,
            computed_at=datetime.now(UTC),
        )
        for index, point in enumerate(history.points[-25:])
    ]

    result = BaselineForecaster(minimum_sentiment_days=10).forecast(history.points, sentiment)

    assert 0 <= result.probability_up <= 1
    assert result.as_of == history.points[-1].date
    assert result.trained_through == history.points[-6].date
    assert result.sentiment_features_used is True
    assert result.metrics.validation_samples >= 30


def test_forecast_excludes_sparse_sentiment_features() -> None:
    history = make_price_history()
    recent = history.points[-1]
    sentiment = [
        DailySentiment(
            ticker="ACME",
            date=recent.date,
            score=0.4,
            moving_average_7d=0.4,
            article_count=1,
            computed_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    ]

    result = BaselineForecaster().forecast(history.points, sentiment)

    assert result.sentiment_features_used is False
    assert all(not feature.startswith("sentiment") for feature in result.features)


def test_sentiment_is_available_on_next_trading_session_not_same_close() -> None:
    history = make_price_history()
    source_point = history.points[-3]
    next_point = history.points[-2]
    sentiment = [
        DailySentiment(
            ticker="ACME",
            date=source_point.date,
            score=0.7,
            moving_average_7d=0.4,
            article_count=3,
            computed_at=datetime.now(UTC),
        )
    ]

    frame = _feature_frame(history.points, sentiment, horizon=5)

    assert frame.loc[str(source_point.date), "sentiment_article_count"] == 0
    assert frame.loc[str(next_point.date), "sentiment_article_count"] == 3
