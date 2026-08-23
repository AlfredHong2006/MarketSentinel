from datetime import UTC, datetime, timedelta

from marketsentinel.dashboard_intelligence import (
    compatible_intelligence_events,
    contradiction_label,
    corroboration_label,
    corroboration_metric,
    evidence_breakdown_label,
    prepare_todays_intelligence,
    primary_source_label,
    summarize_corroboration,
)
from marketsentinel.domain import (
    ArticleEvidenceReference,
    ClaimAssessment,
    CompanyIntelligenceEvent,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    EvidenceStatus,
    SourceClass,
    TimeHorizon,
)

PUBLISHED = datetime(2026, 8, 15, tzinfo=UTC)


def evidence_reference(
    article_id: str,
    *,
    publisher: str = "Second Wire",
    title: str | None = None,
    published_at: datetime | None = None,
) -> ArticleEvidenceReference:
    return ArticleEvidenceReference(
        article_id=article_id,
        title=title or f"Corroborating source {article_id}",
        publisher=publisher,
        published_at=published_at or datetime(2026, 8, 14, tzinfo=UTC),
        url=f"https://example.com/{article_id}",
    )


def claim(
    claim_id: str,
    status: EvidenceStatus,
    *,
    evidence_article_ids: list[str] | None = None,
) -> ClaimAssessment:
    return ClaimAssessment(
        claim_id=claim_id,
        status=status,
        reasoning="Deterministic test assessment.",
        evidence_article_ids=evidence_article_ids or [],
        confidence=0.8,
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
    claims: list[ClaimAssessment] | None = None,
    publisher: str = "Test Wire",
    title: str | None = None,
) -> CompanyIntelligenceEvent:
    publication = published_at or PUBLISHED
    return CompanyIntelligenceEvent(
        article_id=article_id,
        source_reference=ArticleEvidenceReference(
            article_id=article_id,
            title=title or f"Acme event {article_id}",
            publisher=publisher,
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
        claims=claims or [],
        evidence_strength=evidence_strength,
        evidence_sources=evidence_sources or [],
    )


def test_todays_intelligence_filters_uncertain_and_low_confidence_records() -> None:
    eligible = intelligence_event("eligible")
    uncertain = intelligence_event("uncertain", event_type=EventType.UNCERTAIN)
    low_confidence = intelligence_event("low-confidence", confidence=0.6)
    low_magnitude = intelligence_event("low-magnitude", magnitude=0.25)

    cards = prepare_todays_intelligence([uncertain, low_confidence, low_magnitude, eligible])

    assert [card.event.article_id for card in cards] == ["eligible"]
    assert cards[0].impact_label == "High impact"


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


def test_supplied_comparison_pool_is_never_described_as_support() -> None:
    """The defect this replaces: five comparison articles rendered as five supporting sources."""

    event = intelligence_event(
        "pool-only",
        evidence_sources=[evidence_reference(f"s{index}") for index in range(5)],
        claims=[claim("claim_1", EvidenceStatus.UNSUPPORTED)],
    )

    card = prepare_todays_intelligence([event])[0]
    rendered = f"{card.corroboration_metric} {card.corroboration_label}".lower()

    assert card.corroboration.comparison_articles == 5
    assert card.corroboration.external_sources == 0
    assert "5" not in rendered
    assert "supporting source" not in rendered
    assert card.corroboration_label == "No external corroboration found"


def test_zero_corroboration_is_explicit_and_never_implies_a_claim_is_false() -> None:
    summary = summarize_corroboration(
        intelligence_event(
            "none",
            evidence_sources=[evidence_reference("s1")],
            claims=[claim("claim_1", EvidenceStatus.UNSUPPORTED)],
        )
    )

    assert corroboration_label(summary) == "No external corroboration found"
    assert corroboration_metric(summary) == "None found"
    assert "false" not in corroboration_label(summary).lower()
    assert "unsupported" not in corroboration_label(summary).lower()


def test_one_corroborating_article_counts_as_one_not_the_whole_pool() -> None:
    event = intelligence_event(
        "one",
        evidence_sources=[evidence_reference(f"s{index}") for index in range(5)],
        claims=[claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s0"])],
    )

    summary = summarize_corroboration(event)

    assert summary.comparison_articles == 5
    assert summary.supporting_articles == 1
    assert summary.external_sources == 1
    assert corroboration_label(summary) == "1 claim corroborated · 1 external source"


def test_several_claims_citing_one_article_are_not_several_sources() -> None:
    event = intelligence_event(
        "shared",
        publisher="Test Wire",
        evidence_sources=[evidence_reference("s1", publisher="Reuters")],
        claims=[
            claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1"]),
            claim("claim_2", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1"]),
            claim("claim_3", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1"]),
        ],
    )

    summary = summarize_corroboration(event)

    assert summary.corroborated_claims == 3
    assert summary.external_sources == 1
    assert corroboration_label(summary) == "3 claims corroborated · 1 external source"


def test_two_distinct_publishers_supporting_one_claim_count_as_two() -> None:
    event = intelligence_event(
        "two",
        evidence_sources=[
            evidence_reference("s1", publisher="Reuters"),
            evidence_reference("s2", publisher="Bloomberg"),
        ],
        claims=[
            claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1", "s2"]),
        ],
    )

    summary = summarize_corroboration(event)

    assert summary.external_sources == 2
    assert corroboration_label(summary) == "1 claim corroborated · 2 external sources"
    assert corroboration_metric(summary) == "2 external sources"


def test_corroboration_from_the_primary_publisher_is_not_an_external_source() -> None:
    event = intelligence_event(
        "self",
        publisher="Financial Times",
        evidence_sources=[evidence_reference("s1", publisher="Financial Times")],
        claims=[claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1"])],
    )

    summary = summarize_corroboration(event)

    assert summary.corroborated_claims == 1
    assert summary.external_sources == 0
    assert corroboration_label(summary) == "Supported only by the same publisher"


def test_official_primary_corroborated_only_by_company_channels_says_so() -> None:
    event = intelligence_event(
        "official-only",
        source_class=SourceClass.OFFICIAL_COMPANY,
        publisher="NVIDIA Blog",
        evidence_sources=[
            evidence_reference("s1", publisher="NVIDIA Newsroom"),
            evidence_reference("s2", publisher="NVIDIA Developer"),
        ],
        claims=[
            claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1", "s2"]),
        ],
    )

    summary = summarize_corroboration(event)

    assert summary.supporting_articles == 2
    assert summary.external_sources == 0
    assert corroboration_label(summary) == "Supported only by the company's own channels"


def test_a_syndicated_rewrite_of_the_primary_is_not_a_second_source() -> None:
    event = intelligence_event(
        "twin",
        publisher="Reuters",
        title="Acme wins record cloud infrastructure contract",
        evidence_sources=[
            evidence_reference(
                "s1",
                publisher="Syndicated Daily",
                title="Acme wins record cloud infrastructure contract",
                published_at=PUBLISHED + timedelta(hours=2),
            )
        ],
        claims=[claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1"])],
    )

    summary = summarize_corroboration(event)

    assert summary.supporting_articles == 1
    assert summary.external_sources == 0
    assert corroboration_label(summary) == "Supported only by the same publisher"


def test_contradiction_is_always_surfaced_and_never_raises_a_count() -> None:
    contradicted_only = summarize_corroboration(
        intelligence_event(
            "contradicted",
            evidence_sources=[evidence_reference("s1", publisher="Reuters")],
            claims=[
                claim("claim_1", EvidenceStatus.CONTRADICTED, evidence_article_ids=["s1"]),
                claim("claim_2", EvidenceStatus.UNSUPPORTED),
            ],
        )
    )
    mixed = summarize_corroboration(
        intelligence_event(
            "mixed",
            evidence_sources=[
                evidence_reference("s1", publisher="Reuters"),
                evidence_reference("s2", publisher="Bloomberg"),
            ],
            claims=[
                claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s1"]),
                claim("claim_2", EvidenceStatus.CONTRADICTED, evidence_article_ids=["s2"]),
            ],
        )
    )

    assert contradiction_label(contradicted_only) == "Conflicting evidence on 1 of 2 claims"
    assert contradicted_only.external_sources == 0, "a contradiction is never support"
    assert corroboration_label(contradicted_only) == "No external corroboration found"
    assert contradiction_label(mixed) == "Conflicting evidence on 1 of 2 claims"
    assert mixed.external_sources == 1, "only the corroborated claim's citation counts"


def test_contradiction_label_is_absent_when_nothing_conflicts() -> None:
    summary = summarize_corroboration(
        intelligence_event("clean", claims=[claim("claim_1", EvidenceStatus.UNSUPPORTED)])
    )

    assert contradiction_label(summary) is None


def test_primary_source_quality_stays_visible_when_nothing_corroborates() -> None:
    filing = intelligence_event(
        "filing",
        source_class=SourceClass.REGULATORY_OR_FILING,
        claims=[claim("claim_1", EvidenceStatus.UNSUPPORTED)],
    )
    opinion = intelligence_event(
        "opinion",
        source_class=SourceClass.COMMENTARY_OR_OPINION,
        claims=[claim("claim_1", EvidenceStatus.UNSUPPORTED)],
    )

    filing_card, opinion_card = (
        prepare_todays_intelligence([filing])[0],
        prepare_todays_intelligence([opinion])[0],
    )

    assert filing_card.primary_source_label == "Regulatory filing"
    assert opinion_card.primary_source_label == "Commentary or opinion"
    assert filing_card.corroboration_label == opinion_card.corroboration_label
    assert filing_card.primary_source_label != opinion_card.primary_source_label


def test_primary_source_label_is_defined_for_every_source_class() -> None:
    labels = {primary_source_label(source_class) for source_class in SourceClass}

    assert len(labels) == len(SourceClass)
    assert "_" not in "".join(labels)


def test_evidence_breakdown_separates_material_examined_from_actual_support() -> None:
    summary = summarize_corroboration(
        intelligence_event(
            "breakdown",
            evidence_sources=[evidence_reference(f"s{index}") for index in range(5)],
            claims=[
                claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s0"]),
                claim("claim_2", EvidenceStatus.CONTRADICTED, evidence_article_ids=["s1"]),
                claim("claim_3", EvidenceStatus.UNSUPPORTED),
            ],
        )
    )

    assert evidence_breakdown_label(summary) == (
        "5 comparison articles evaluated · 1 of 3 claims corroborated · 1 contradicted "
        "· 1 unsupported or uncertain"
    )


def test_a_stored_shaped_record_renders_without_new_fields() -> None:
    """Historical rows carry exactly these fields, so corrected wording needs no re-analysis."""

    record = intelligence_event(
        "stored",
        evidence_sources=[evidence_reference(f"s{index}") for index in range(5)],
        claims=[claim("claim_1", EvidenceStatus.CORROBORATED, evidence_article_ids=["s0"])],
    ).model_dump(mode="json")

    events = compatible_intelligence_events([record])
    cards = prepare_todays_intelligence(events)

    assert len(cards) == 1
    assert cards[0].corroboration_label == "1 claim corroborated · 1 external source"


def test_no_wording_claims_independence_that_the_data_cannot_establish() -> None:
    summaries = [
        summarize_corroboration(
            intelligence_event(
                "any",
                evidence_sources=[evidence_reference("s1", publisher="Reuters")],
                claims=[claim("claim_1", status, evidence_article_ids=ids)],
            )
        )
        for status, ids in (
            (EvidenceStatus.CORROBORATED, ["s1"]),
            (EvidenceStatus.CONTRADICTED, ["s1"]),
            (EvidenceStatus.UNSUPPORTED, None),
        )
    ]

    for summary in summaries:
        rendered = " ".join(
            filter(
                None,
                [
                    corroboration_label(summary),
                    corroboration_metric(summary),
                    contradiction_label(summary),
                    evidence_breakdown_label(summary),
                ],
            )
        ).lower()
        assert "independent" not in rendered
        assert "supporting source" not in rendered
