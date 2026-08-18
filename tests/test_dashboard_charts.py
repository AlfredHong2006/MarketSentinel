from datetime import date

import pandas as pd
import pytest

from marketsentinel.dashboard_charts import (
    build_combined_figure,
    observed_sentiment_frame,
    price_frame_for_timeframe,
    select_meaningful_events,
)


def price_points() -> list[dict[str, object]]:
    return [
        {"date": timestamp.date(), "close": 100.0 + index, "volume": 1_000_000.0}
        for index, timestamp in enumerate(pd.date_range("2025-01-02", "2026-01-15", freq="B"))
    ]


@pytest.mark.parametrize(
    ("timeframe", "months"),
    [("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12)],
)
def test_price_timeframes_use_calendar_windows(timeframe: str, months: int) -> None:
    frame = price_frame_for_timeframe(price_points(), timeframe)

    expected_cutoff = frame["date"].max() - pd.DateOffset(months=months)
    assert frame["date"].min() >= expected_cutoff
    assert frame.iloc[-1]["date"] == pd.Timestamp("2026-01-15")


def test_six_month_chart_receives_more_than_a_30_session_source_history() -> None:
    frame = price_frame_for_timeframe(price_points(), "6M")

    assert len(frame) > 120


def test_sentiment_filter_preserves_only_observed_dates() -> None:
    values = [
        {
            "date": date(2026, 1, day),
            "score": score,
            "trend_3": score,
            "article_count": 1,
            "positive_share": 0.5,
            "negative_share": 0.2,
            "weighted_disagreement": 0.1,
        }
        for day, score in [(2, 0.2), (8, -0.1), (14, 0.4)]
    ]

    frame = observed_sentiment_frame(
        values,
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-15"),
    )

    assert frame["date"].dt.day.tolist() == [2, 8, 14]
    assert len(frame) == len(values)


def test_event_markers_are_real_material_bounded_analyses() -> None:
    base = {
        "article_id": "article",
        "event_date": date(2026, 1, 10),
        "article_url": "https://example.com/article",
        "event_type": "partnership",
        "summary": "A genuine stored event.",
        "direction": "positive",
        "magnitude": 0.6,
        "extraction_confidence": 0.8,
        "evidence_strength": 0.7,
    }
    events = [
        base,
        {**base, "article_id": "low-impact", "magnitude": 0.1},
        {**base, "article_id": "low-confidence", "extraction_confidence": 0.3},
        {**base, "article_id": "uncertain", "event_type": "uncertain"},
        {**base, "article_id": "outside", "event_date": date(2025, 1, 10)},
    ]

    selected = select_meaningful_events(
        events,
        pd.Timestamp("2026-01-01"),
        pd.Timestamp("2026-01-15"),
    )

    assert [item["article_id"] for item in selected] == ["article"]


def test_combined_chart_keeps_price_sentiment_and_events_distinct() -> None:
    prices = price_frame_for_timeframe(price_points(), "1M")
    sentiment = observed_sentiment_frame(
        [
            {
                "date": date(2026, 1, 8),
                "score": 0.25,
                "trend_3": 0.25,
                "article_count": 2,
                "positive_share": 0.7,
                "negative_share": 0.1,
                "weighted_disagreement": 0.2,
            }
        ],
        prices["date"].min(),
        prices["date"].max(),
    )
    events = [
        {
            "article_id": "article",
            "event_date": pd.Timestamp("2026-01-10"),
            "article_url": "https://example.com/article",
            "event_type": "earnings",
            "summary": "A stored earnings event.",
            "direction": "mixed",
            "magnitude": 0.7,
            "extraction_confidence": 0.8,
            "evidence_strength": 0.6,
        }
    ]

    figure = build_combined_figure(prices, sentiment, events, {"Price", "Sentiment", "Events"})

    assert [trace.name for trace in figure.data] == [
        "Price",
        "Analysed events",
        "News sentiment",
    ]
    sentiment_trace = next(trace for trace in figure.data if trace.name == "News sentiment")
    assert len(sentiment_trace.x) == 1
