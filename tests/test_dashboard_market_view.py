from datetime import UTC, datetime

from marketsentinel.dashboard_intelligence import prepare_todays_intelligence
from marketsentinel.dashboard_market_view import (
    NO_INTELLIGENCE_MESSAGE,
    NO_PRICE_MESSAGE,
    NO_RISK_MESSAGE,
    NO_SENTIMENT_MESSAGE,
    build_market_view,
)
from marketsentinel.dashboard_risks import prepare_top_risk_rows
from marketsentinel.domain import (
    ArticleEvidenceReference,
    CompanyIntelligenceEvent,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    RankedRisk,
    RiskTheme,
    SourceClass,
    TimeHorizon,
)

PRICE_POINTS = [
    {"date": "2026-08-19", "close": 100.0, "volume": 1_000_000.0},
    {"date": "2026-08-20", "close": 105.0, "volume": 1_000_000.0},
]

DAILY_SENTIMENT = [
    {
        "ticker": "ACME",
        "date": "2026-08-20",
        "score": 0.3,
        "moving_average_7d": 0.22,
        "trend_3": 0.1,
        "article_count": 4,
        "positive_share": 0.6,
        "negative_share": 0.1,
        "weighted_disagreement": 0.2,
        "aggregate_weight": 3.0,
        "computed_at": "2026-08-20T12:00:00Z",
    }
]


def _ranked_risk() -> RankedRisk:
    return RankedRisk(
        theme=RiskTheme.EXPORT_TRADE,
        concern_index=42,
        band="Moderate",
        summary="Government restrictions remain the strongest currently evidenced concern.",
        primary_article_id="article-1",
        primary_article_url="https://example.com/article-1",
        primary_publisher="Test Wire",
        first_evidenced_at=datetime(2026, 8, 10, tzinfo=UTC),
        latest_published_at=datetime(2026, 8, 15, tzinfo=UTC),
        supporting_article_ids=["article-1"],
        supporting_publishers=["Test Wire"],
        supporting_signal_count=1,
        supporting_event_group_count=1,
    )


def _intelligence_event() -> CompanyIntelligenceEvent:
    return CompanyIntelligenceEvent(
        article_id="article-1",
        source_reference=ArticleEvidenceReference(
            article_id="article-1",
            title="Acme wins a major contract",
            publisher="Test Wire",
            published_at=datetime(2026, 8, 15, tzinfo=UTC),
            url="https://example.com/article-1",
        ),
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=EventExtraction(
            event_type=EventType.CONTRACT_AWARD,
            summary="Acme won a material customer contract.",
            direction=EventDirection.POSITIVE,
            magnitude=0.65,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=0.8,
        ),
        evidence_strength=0.6,
    )


def test_fully_populated_view_states_each_observation_independently() -> None:
    risk_rows = prepare_top_risk_rows([_ranked_risk()])
    cards = prepare_todays_intelligence([_intelligence_event()])

    summary = build_market_view(
        price_points=PRICE_POINTS,
        daily_sentiment=DAILY_SENTIMENT,
        risk_rows=risk_rows,
        intelligence_cards=cards,
    )

    assert "105.00" in summary.price_note
    assert "up" in summary.price_note
    assert "positive" in summary.sentiment_note
    assert "Concern Index of 42" in summary.risk_note
    assert "Moderate" in summary.risk_note
    assert "positive" in summary.intelligence_note
    assert "contract award" in summary.intelligence_note


def test_view_never_invents_a_composite_verdict_or_causal_claim() -> None:
    risk_rows = prepare_top_risk_rows([_ranked_risk()])
    cards = prepare_todays_intelligence([_intelligence_event()])

    summary = build_market_view(
        price_points=PRICE_POINTS,
        daily_sentiment=DAILY_SENTIMENT,
        risk_rows=risk_rows,
        intelligence_cards=cards,
    )

    combined = " ".join(
        [summary.price_note, summary.sentiment_note, summary.risk_note, summary.intelligence_note]
    ).lower()
    for forbidden in ("bullish", "bearish", "buy", "sell", "outlook", "driven by", "because"):
        assert forbidden not in combined


def test_missing_sentiment_degrades_without_fabricating_a_value() -> None:
    summary = build_market_view(
        price_points=PRICE_POINTS,
        daily_sentiment=[],
        risk_rows=prepare_top_risk_rows([_ranked_risk()]),
        intelligence_cards=prepare_todays_intelligence([_intelligence_event()]),
    )

    assert summary.sentiment_note == NO_SENTIMENT_MESSAGE


def test_missing_risks_degrades_without_fabricating_a_value() -> None:
    summary = build_market_view(
        price_points=PRICE_POINTS,
        daily_sentiment=DAILY_SENTIMENT,
        risk_rows=[],
        intelligence_cards=prepare_todays_intelligence([_intelligence_event()]),
    )

    assert summary.risk_note == NO_RISK_MESSAGE


def test_missing_intelligence_degrades_without_fabricating_a_value() -> None:
    summary = build_market_view(
        price_points=PRICE_POINTS,
        daily_sentiment=DAILY_SENTIMENT,
        risk_rows=prepare_top_risk_rows([_ranked_risk()]),
        intelligence_cards=[],
    )

    assert summary.intelligence_note == NO_INTELLIGENCE_MESSAGE


def test_fully_empty_state_is_honest_about_missing_data() -> None:
    summary = build_market_view(
        price_points=[], daily_sentiment=[], risk_rows=[], intelligence_cards=[]
    )

    assert summary.price_note == NO_PRICE_MESSAGE
    assert summary.sentiment_note == NO_SENTIMENT_MESSAGE
    assert summary.risk_note == NO_RISK_MESSAGE
    assert summary.intelligence_note == NO_INTELLIGENCE_MESSAGE


def test_sparse_sentiment_never_implies_a_multi_month_trend() -> None:
    summary = build_market_view(
        price_points=PRICE_POINTS,
        daily_sentiment=DAILY_SENTIMENT,
        risk_rows=[],
        intelligence_cards=[],
    )

    assert "6M" not in summary.sentiment_note
    assert "1Y" not in summary.sentiment_note
    assert "short-run" in summary.sentiment_note
