from datetime import UTC, datetime, timedelta

from marketsentinel.dashboard_intelligence import (
    compatible_intelligence_events,
    corroboration_label,
    evidence_label,
    prepare_todays_intelligence,
)
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


def intelligence_event(
    article_id: str,
    *,
    magnitude: float = 0.65,
    confidence: float = 0.8,
    evidence_strength: float = 0.5,
    source_class: SourceClass = SourceClass.MAJOR_FINANCIAL_NEWS,
    published_at: datetime | None = None,
    event_type: EventType = EventType.CONTRACT_AWARD,
    evidence_sources: list[ArticleEvidenceReference] | None = None,
) -> CompanyIntelligenceEvent:
    publication = published_at or datetime(2026, 8, 15, tzinfo=UTC)
    return CompanyIntelligenceEvent(
        article_id=article_id,
        source_reference=ArticleEvidenceReference(
            article_id=article_id,
            title=f"Acme event {article_id}",
            publisher="Test Wire",
            published_at=publication,
            url=f"https://example.com/{article_id}",
        ),
        source_class=source_class,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=EventExtraction(
            event_type=event_type,
            summary="Acme won a material customer contract.",
            direction=EventDirection.POSITIVE,
            magnitude=magnitude,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=confidence,
            important_claims=["Acme announced a contract award."],
            positive_channels=["Possible demand expansion"],
        ),
        evidence_strength=evidence_strength,
        evidence_sources=evidence_sources or [],
    )


def evidence_reference(article_id: str) -> ArticleEvidenceReference:
    return ArticleEvidenceReference(
        article_id=article_id,
        title=f"Corroborating source {article_id}",
        publisher="Second Wire",
        published_at=datetime(2026, 8, 14, tzinfo=UTC),
        url=f"https://example.com/{article_id}",
    )


def test_todays_intelligence_filters_uncertain_and_low_confidence_records() -> None:
    eligible = intelligence_event("eligible")
    uncertain = intelligence_event("uncertain", event_type=EventType.UNCERTAIN)
    low_confidence = intelligence_event("low-confidence", confidence=0.6)
    low_magnitude = intelligence_event("low-magnitude", magnitude=0.25)

    cards = prepare_todays_intelligence([uncertain, low_confidence, low_magnitude, eligible])

    assert [card.event.article_id for card in cards] == ["eligible"]
    assert cards[0].impact_label == "High impact"
    assert cards[0].evidence_label == "Moderate evidence"


def test_todays_intelligence_order_is_deterministic_and_explained_by_quality_signals() -> None:
    older = intelligence_event(
        "older",
        magnitude=0.65,
        confidence=0.8,
        evidence_strength=0.5,
        published_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    stronger_evidence = intelligence_event(
        "stronger-evidence",
        magnitude=0.65,
        confidence=0.8,
        evidence_strength=0.7,
        published_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    more_material = intelligence_event(
        "more-material",
        magnitude=0.8,
        confidence=0.7,
        evidence_strength=0.4,
    )

    cards = prepare_todays_intelligence([older, stronger_evidence, more_material])

    assert [card.event.article_id for card in cards] == [
        "more-material",
        "stronger-evidence",
        "older",
    ]


def test_incompatible_record_with_missing_evidence_strength_is_excluded_not_fabricated() -> None:
    record = intelligence_event("legacy").model_dump(mode="json")
    record.pop("evidence_strength")

    assert compatible_intelligence_events([record]) == []
    assert evidence_label(None) is None


def test_source_class_breaks_otherwise_equal_intelligence_ties() -> None:
    published = datetime.now(UTC) - timedelta(days=1)
    official = intelligence_event(
        "official",
        source_class=SourceClass.OFFICIAL_COMPANY,
        published_at=published,
    )
    general = intelligence_event(
        "general",
        source_class=SourceClass.GENERAL_NEWS,
        published_at=published,
    )

    cards = prepare_todays_intelligence([general, official])

    assert [card.event.article_id for card in cards] == ["official", "general"]


def test_corroboration_count_matches_supplied_evidence_sources_exactly() -> None:
    no_sources = intelligence_event("no-sources", evidence_sources=[])
    one_source = intelligence_event("one-source", evidence_sources=[evidence_reference("s1")])
    two_sources = intelligence_event(
        "two-sources", evidence_sources=[evidence_reference("s1"), evidence_reference("s2")]
    )

    cards = prepare_todays_intelligence([no_sources, one_source, two_sources])
    by_id = {card.event.article_id: card for card in cards}

    assert by_id["no-sources"].corroboration_count == 0
    assert by_id["one-source"].corroboration_count == 1
    assert by_id["two-sources"].corroboration_count == 2


def test_corroboration_label_never_implies_a_claim_is_false() -> None:
    assert corroboration_label(0) == "No additional supplied sources"
    assert "false" not in corroboration_label(0).lower()
    assert "unsupported" not in corroboration_label(0).lower()
    assert corroboration_label(1) == "1 supporting source"
    assert corroboration_label(3) == "3 supporting sources"
