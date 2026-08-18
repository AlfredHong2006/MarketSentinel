from datetime import UTC, datetime

from marketsentinel.dashboard_charts import (
    build_combined_figure,
    price_frame_for_timeframe,
    select_meaningful_events,
)
from marketsentinel.dashboard_event_state import updated_event_markers, updated_intelligence_events
from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleAnalysisResponse,
    ArticleEvidenceReference,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    SourceClass,
    TimeHorizon,
)


def _fresh_analysis_response() -> dict[str, object]:
    analysis = ArticleAnalysis(
        article_id="article-1",
        source_reference=ArticleEvidenceReference(
            article_id="article-1",
            title="Acme wins a major contract",
            publisher="Test Wire",
            published_at=datetime(2026, 1, 15, tzinfo=UTC),
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
        evidence_count=0,
        evidence_strength=0.4,
        evidence_fingerprint="evidence-fingerprint",
        model_version="test-model",
        stage_a_prompt_version="a",
        stage_b_prompt_version="b",
        stage_c_prompt_version="c",
        schema_version="article-intelligence-v4",
        analysis_created_at=datetime(2026, 1, 15, tzinfo=UTC),
    )
    return ArticleAnalysisResponse(
        article_id=analysis.article_id,
        status="generated",
        analysis=analysis,
    ).model_dump(mode="json")


def _price_points() -> list[dict[str, object]]:
    return [
        {"date": f"2026-01-{day:02d}", "close": 100.0 + day, "volume": 1_000_000.0}
        for day in range(2, 31)
    ]


def test_fresh_eligible_analysis_updates_state_and_events_layer_renders_marker() -> None:
    response = _fresh_analysis_response()
    markers = updated_event_markers(response, "ACME", [])
    intelligence = updated_intelligence_events(response, "ACME", [])

    assert markers is not None
    assert [item["article_id"] for item in markers] == ["article-1"]
    assert intelligence is not None
    assert [item["article_id"] for item in intelligence] == ["article-1"]
    prices = price_frame_for_timeframe(_price_points(), "1M")
    events = select_meaningful_events(markers, prices["date"].min(), prices["date"].max())
    events_enabled = build_combined_figure(prices, prices.iloc[0:0], events, {"Price", "Events"})
    events_disabled = build_combined_figure(prices, prices.iloc[0:0], events, {"Price"})

    assert "Analysed events" in [trace.name for trace in events_enabled.data]
    assert "Analysed events" not in [trace.name for trace in events_disabled.data]


def test_incompatible_legacy_response_does_not_update_dashboard_markers() -> None:
    legacy_response = _fresh_analysis_response()
    legacy_response["analysis"].pop("evidence_strength")

    assert updated_event_markers(legacy_response, "ACME", []) is None
