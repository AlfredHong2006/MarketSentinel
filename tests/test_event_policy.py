from datetime import UTC, datetime

from marketsentinel.dashboard_charts import select_meaningful_events
from marketsentinel.dashboard_intelligence import is_intelligence_eligible
from marketsentinel.domain import (
    ArticleEvidenceReference,
    CompanyIntelligenceEvent,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.event_policy import is_meaningful_event, is_meaningful_event_values


def _event(
    *, magnitude: float = 0.30, confidence: float = 0.65, event_type: EventType = EventType.EARNINGS
) -> EventExtraction:
    return EventExtraction(
        event_type=event_type,
        summary="Acme announced earnings.",
        direction=EventDirection.POSITIVE,
        magnitude=magnitude,
        time_horizon=TimeHorizon.DAYS,
        model_confidence=confidence,
        important_claims=["Acme announced earnings."],
    )


def _intelligence_event(event: EventExtraction) -> CompanyIntelligenceEvent:
    return CompanyIntelligenceEvent(
        article_id="article",
        source_reference=ArticleEvidenceReference(
            article_id="article",
            title="Acme reports earnings",
            publisher="Test Wire",
            published_at=datetime(2026, 8, 18, tzinfo=UTC),
            url="https://example.com/article",
        ),
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=event,
        evidence_strength=0.5,
    )


def test_meaningful_event_policy_has_explicit_materiality_and_confidence_boundaries() -> None:
    assert not is_meaningful_event(_event(magnitude=0.25))
    assert is_meaningful_event(_event())
    assert not is_meaningful_event(_event(confidence=0.60))
    assert not is_meaningful_event(_event(event_type=EventType.UNCERTAIN))
    assert not is_meaningful_event(_event(), is_external_institutional_holding=True)


def test_chart_and_intelligence_reuse_the_meaningful_event_semantic_base() -> None:
    event = _event()
    marker = {
        "article_id": "article",
        "event_date": "2026-08-18",
        "article_url": "https://example.com/article",
        "event_type": event.event_type.value,
        "summary": event.summary,
        "direction": event.direction.value,
        "magnitude": event.magnitude,
        "extraction_confidence": event.model_confidence,
        "evidence_strength": 0.5,
    }

    assert is_meaningful_event_values(event.event_type, event.magnitude, event.model_confidence)
    assert len(select_meaningful_events([marker], datetime(2026, 8, 1), datetime(2026, 8, 31))) == 1
    assert is_intelligence_eligible(_intelligence_event(event))


def test_claimless_meaningful_event_remains_eligible_for_chart_and_intelligence() -> None:
    event = _event()
    event = event.model_copy(update={"important_claims": []})
    marker = {
        "article_id": "article",
        "event_date": "2026-08-18",
        "article_url": "https://example.com/article",
        "event_type": event.event_type.value,
        "summary": event.summary,
        "direction": event.direction.value,
        "magnitude": event.magnitude,
        "extraction_confidence": event.model_confidence,
        "evidence_strength": 0.5,
    }

    assert len(select_meaningful_events([marker], datetime(2026, 8, 1), datetime(2026, 8, 31))) == 1
    assert is_intelligence_eligible(_intelligence_event(event))
