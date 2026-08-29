"""Semantics of the deterministic materiality gate, grouping, and ranking.

These tests pin behaviour rather than numbers: which condition rejects a row and why, when two
reports are one development, and what the ordering promises. The corpus-scale confusion matrix is
a separate, later concern -- nothing here reads the database, and nothing here is a claim about
how a real corpus is distributed.
"""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from marketsentinel.analysis_candidates import has_financial_disclosure_signal, has_priority_signal
from marketsentinel.dashboard_intelligence import (
    contradiction_label,
    corroboration_metric,
    impact_label,
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
from marketsentinel.materiality import (
    DRIVER_EVENT_TYPE,
    DRIVER_NOT_MEANINGFUL,
    DURABILITY,
    EVIDENCE,
    GUARD_COMMENTARY,
    GUARD_MARKET_MOVE,
    GUARD_PRICE_MOVE,
    GUARD_THIRD_PARTY_APPOINTMENT,
    REJECTION_CONDITIONS,
    SYNDICATION_OVERLAP,
    TIER_CAPITAL_OR_OPERATIONS,
    TIER_DISCLOSURE_OR_LEGAL,
    TIER_PRODUCT_OR_PARTNERSHIP,
    anchor_terms,
    assess_materiality,
    describes_percent_price_move,
    describes_same_material_event,
    group_material_events,
    is_material,
    prepare_key_developments,
)
from marketsentinel.risk_scoring import title_terms

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
ACME = CompanyReference(symbol="ACME", name="Acme Corporation")
# A second subject, because the third-party-appointment guard is the one rule that reads the
# company under analysis rather than the title alone.
PFIZER = CompanyReference(symbol="PFE", name="Pfizer")
NEAR_DUPLICATE_TITLE = "chipmaker wins new supply deal"


def event(
    article_id: str,
    *,
    title: str = "Acme acquires rival chipmaker Bolt for $4 billion",
    event_type: EventType = EventType.ACQUISITION,
    direction: EventDirection = EventDirection.POSITIVE,
    magnitude: float = 0.55,
    confidence: float = 0.85,
    horizon: TimeHorizon = TimeHorizon.MONTHS,
    source_class: SourceClass = SourceClass.MAJOR_FINANCIAL_NEWS,
    publisher: str = "Reuters",
    hours_old: int = 0,
    external_publishers: tuple[str, ...] = (),
    contradicted: bool = False,
    subject: CompanyReference = ACME,
) -> CompanyIntelligenceEvent:
    published_at = NOW - timedelta(hours=hours_old)
    evidence_sources: list[ArticleEvidenceReference] = []
    claims: list[ClaimAssessment] = []
    for index, name in enumerate(external_publishers):
        evidence_sources.append(
            reference(f"{article_id}-e{index}", f"Independent desk {index} filed on it", name)
        )
    if evidence_sources:
        claims.append(
            assessment(
                "c1", EvidenceStatus.CORROBORATED, [item.article_id for item in evidence_sources]
            )
        )
    if contradicted:
        disputed = reference(f"{article_id}-d", "A rival desk disputes the reported size", "CNBC")
        evidence_sources.append(disputed)
        claims.append(assessment("c2", EvidenceStatus.CONTRADICTED, [disputed.article_id]))
    return CompanyIntelligenceEvent(
        article_id=article_id,
        source_reference=reference(article_id, title, publisher, published_at=published_at),
        source_class=source_class,
        subject_company=subject,
        event=EventExtraction(
            event_type=event_type,
            summary="A deterministic test extraction.",
            direction=direction,
            magnitude=magnitude,
            time_horizon=horizon,
            model_confidence=confidence,
            important_claims=["Acme did the thing the title reports."],
        ),
        claims=claims,
        evidence_strength=0.5,
        evidence_sources=evidence_sources,
    )


def reference(
    article_id: str,
    title: str,
    publisher: str,
    *,
    published_at: datetime | None = None,
) -> ArticleEvidenceReference:
    return ArticleEvidenceReference(
        article_id=article_id,
        title=title,
        publisher=publisher,
        published_at=published_at or NOW - timedelta(hours=1),
        url=f"https://example.com/{article_id}",
    )


def assessment(claim_id: str, status: EvidenceStatus, evidence: list[str]) -> ClaimAssessment:
    return ClaimAssessment(
        claim_id=claim_id,
        status=status,
        reasoning="Deterministic test verdict.",
        evidence_article_ids=evidence,
        confidence=0.8,
    )


def overlap(first: str, second: str) -> float:
    left, right = title_terms(first), title_terms(second)
    return len(left & right) / min(len(left), len(right))


def test_commentary_and_price_reaction_titles_are_rejected_before_any_other_condition() -> None:
    commentary = event("why", title="Why Acme's next quarter could disappoint")
    market_move = event("shares", title="Acme shares surge to a record after the deal")
    price_move = event("pct", title="Supermicro drops 33% after co-founder charged over chips")

    assert assess_materiality(commentary).failed_condition == GUARD_COMMENTARY
    assert assess_materiality(market_move).failed_condition == GUARD_MARKET_MOVE
    assert assess_materiality(price_move).failed_condition == GUARD_PRICE_MOVE
    assert not assess_materiality(commentary).passes_guard


def test_another_organisation_hiring_a_company_executive_is_not_a_company_development() -> None:
    """The subject is the departing executive's employer, not a party to the appointment."""

    poached = event(
        "nike",
        title="Watch Nike Appoints Pfizer CFO as New Finance Chief Amid Industry Experience Questions",
        event_type=EventType.MANAGEMENT_CHANGE,
        direction=EventDirection.MIXED,
        magnitude=0.30,
        confidence=0.75,
        horizon=TimeHorizon.UNCERTAIN,
        subject=PFIZER,
    )

    assessment = assess_materiality(poached)

    assert assessment.failed_condition == GUARD_THIRD_PARTY_APPOINTMENT
    assert not assessment.passes_guard


@pytest.mark.parametrize(
    ("article_id", "title"),
    (
        ("viiv", "Shionogi Acquires $2.1 Billion ViiV Healthcare Shareholdings from Pfizer"),
        ("sanofi", "Sanofi sues Pfizer, Moderna over COVID shot technology"),
        ("metsera", "Novo Nordisk Seeks to Outmuscle Pfizer With $9 Billion Bid for Metsera"),
    ),
)
def test_a_third_party_headline_subject_never_rejects_a_principal(
    article_id: str, title: str
) -> None:
    """Seller, defendant, and rival bidder are all principals, however the headline reads."""

    assert is_material(event(article_id, title=title, subject=PFIZER))


def test_percent_guard_separates_price_moves_from_operating_percentages() -> None:
    """The guard must read the move, not the number: operating percentages are the event."""

    assert describes_percent_price_move("Supermicro drops 33% after a smuggling charge")
    assert describes_percent_price_move("Acme shares tumble 12% on the guidance cut")
    assert not describes_percent_price_move("Acme data centre revenue up 75% year on year")
    assert not describes_percent_price_move("Acme AVO Reaches 100% on the ARC-AGI-3 benchmark")


def test_percent_guard_reads_the_metric_a_move_verb_reports_on() -> None:
    """A move verb applied to a reported metric is the disclosure, whichever way it moved."""

    rising = (
        "Acme quarterly profit surges 59% on data centre demand",
        "Acme revenue jumps 40% as AI demand booms",
        "Acme data center sales climb 22% in third quarter",
        "Acme revenues jump 40% on AI demand",
        "Acme profits surge 59%",
    )
    falling = (
        "Acme deliveries fell 12% in China",
        "Acme gross margin slipped 2 percentage points",
        "Acme automotive earnings drop 8% year on year",
    )
    for title in (*rising, *falling):
        assert not describes_percent_price_move(title), title


def test_percent_guard_still_rejects_a_price_reaction_beside_a_metric() -> None:
    """The veto reads one match, never the title: a reaction anywhere still decides it."""

    assert describes_percent_price_move("Acme revenue jumps 40% but shares drop 5%")
    assert describes_percent_price_move("Supermicro drops 33% after co-founder charged over chips")


def test_rows_below_the_shared_meaningful_event_floor_are_not_material() -> None:
    dividend = event(
        "dividend",
        title="Acme fails to dazzle investors despite lifting dividends",
        event_type=EventType.OTHER,
        magnitude=0.10,
    )

    assert assess_materiality(dividend).failed_condition == DRIVER_NOT_MEANINGFUL


def test_other_rescue_reads_disclosure_vocabulary_not_the_broader_priority_signal() -> None:
    """Advocacy about an action matches the priority signal but discloses nothing itself."""

    title = "US must suspend Acme AI chip exports to China, senators say"
    assert has_priority_signal(title)
    assert not has_financial_disclosure_signal(title)

    verdict = assess_materiality(event("letter", title=title, event_type=EventType.OTHER))

    assert not verdict.material
    assert not verdict.rescued_disclosure
    assert verdict.failed_condition == DRIVER_EVENT_TYPE


def test_untyped_periodic_disclosure_is_rescued_as_a_first_tier_development() -> None:
    verdict = assess_materiality(
        event(
            "results",
            title="Acme Announces Financial Results for Second Quarter Fiscal 2026",
            event_type=EventType.OTHER,
            source_class=SourceClass.OFFICIAL_COMPANY,
            publisher="Acme Newsroom",
        )
    )

    assert verdict.material
    assert verdict.rescued_disclosure
    assert verdict.tier == TIER_DISCLOSURE_OR_LEGAL


def test_scheduling_notices_announce_no_result_and_stay_immaterial() -> None:
    title = "Acme Sets Conference Call for Third-Quarter Financial Results"
    assert not has_financial_disclosure_signal(title)

    verdict = assess_materiality(
        event(
            "call",
            title=title,
            event_type=EventType.OTHER,
            source_class=SourceClass.OFFICIAL_COMPANY,
            publisher="Acme Newsroom",
        )
    )

    assert verdict.failed_condition == DRIVER_EVENT_TYPE


def test_uncertain_horizon_passes_only_for_inherently_durable_event_types() -> None:
    suit = event(
        "suit",
        title="Acme sued by Jamendo over AI training data",
        event_type=EventType.LITIGATION,
        direction=EventDirection.NEGATIVE,
        horizon=TimeHorizon.UNCERTAIN,
    )
    talks = event(
        "talks",
        title="Acme in talks with chip startup Rebellions for a potential deal",
        event_type=EventType.PARTNERSHIP,
        horizon=TimeHorizon.UNCERTAIN,
    )

    assert is_material(suit)
    assert assess_materiality(talks).failed_condition == DURABILITY
    assert assess_materiality(talks).passes_driver


def test_official_editorial_needs_support_while_a_capital_commitment_stands_alone() -> None:
    """A company is first-hand on its own money; its newsroom is not evidence for its own launch."""

    launch = event(
        "launch",
        title="Acme DGX Station Puts a Trillion-Parameter Supercomputer on Every Desk",
        event_type=EventType.PRODUCT_LAUNCH,
        source_class=SourceClass.OFFICIAL_COMPANY,
        publisher="Acme Newsroom",
    )
    commitment = event(
        "commitment",
        title="Acme and US Government to Boost AI Infrastructure and R&D Investments",
        event_type=EventType.INVESTMENT,
        source_class=SourceClass.OFFICIAL_COMPANY,
        publisher="Acme Newsroom",
    )
    supported = event(
        "supported",
        title="Acme Ecosystem Expands as Marvell Joins Forces Through NVLink Fusion",
        event_type=EventType.PARTNERSHIP,
        source_class=SourceClass.OFFICIAL_COMPANY,
        publisher="Acme Newsroom",
        external_publishers=("Bloomberg",),
    )

    assert assess_materiality(launch).failed_condition == EVIDENCE
    assert assess_materiality(launch).passes_durability
    assert assess_materiality(commitment).tier == TIER_CAPITAL_OR_OPERATIONS
    assert is_material(supported)


def test_contradicted_claims_are_flagged_on_the_development_never_excluded() -> None:
    disputed = event("disputed", external_publishers=("Bloomberg",), contradicted=True)

    row = prepare_key_developments([disputed]).rows[0]

    assert is_material(disputed)
    assert row.contradiction_label is not None


def test_key_development_rows_reuse_existing_evidence_wording_verbatim() -> None:
    item = event("row", external_publishers=("Bloomberg", "Financial Times"))

    row = prepare_key_developments([item]).rows[0]
    summary = summarize_corroboration(item)

    assert row.corroboration_metric == corroboration_metric(summary)
    assert row.contradiction_label == contradiction_label(summary)
    assert row.impact_label == impact_label(item.event.magnitude)
    assert row.provenance_note == "1 report · 1 publisher"


def test_every_verdict_names_either_a_tier_or_the_condition_that_failed() -> None:
    items = [
        event("material"),
        event("commentary", title="Why Acme's next quarter could disappoint"),
        event("weak", magnitude=0.10),
        event("untyped", event_type=EventType.OTHER),
        event("fleeting", event_type=EventType.PARTNERSHIP, horizon=TimeHorizon.DAYS),
        event(
            "unevidenced",
            title="Acme Unveils Its Next Workstation",
            event_type=EventType.PRODUCT_LAUNCH,
            source_class=SourceClass.OFFICIAL_COMPANY,
            publisher="Acme Newsroom",
        ),
    ]

    for item in items:
        verdict = assess_materiality(item)
        if verdict.material:
            assert verdict.failed_condition is None
            assert verdict.reasons == ()
            assert verdict.tier in {1, 2, 3}
        else:
            assert verdict.failed_condition in REJECTION_CONDITIONS
            assert verdict.reasons and all(verdict.reasons)
            assert verdict.tier is None


def test_anchor_terms_name_the_story_and_never_the_subject_company() -> None:
    anchors = anchor_terms("Acme to Invest $1 Billion in Nokia in AI Networking Push", ACME)

    assert "nokia" in anchors
    assert "acme" not in anchors
    assert "ai" not in anchors


def test_identical_titles_group_even_when_no_anchor_term_exists() -> None:
    """Byte-identical low-signal titles are one story; requiring an anchor would split them."""

    assert anchor_terms(NEAR_DUPLICATE_TITLE, ACME) == frozenset()
    first = event("dup-a", title=NEAR_DUPLICATE_TITLE, event_type=EventType.CONTRACT_AWARD)
    second = event(
        "dup-b",
        title=NEAR_DUPLICATE_TITLE,
        event_type=EventType.CONTRACT_AWARD,
        publisher="Bloomberg",
        hours_old=2,
    )

    groups = group_material_events([first, second])

    assert describes_same_material_event(first, second)
    assert len(groups) == 1
    assert groups[0].publisher_count == 2


def test_independent_reports_of_one_deal_group_on_a_shared_anchor() -> None:
    titles = (
        "Acme to provide up to $105 billion guarantee for OpenAI's Ohio data center",
        "Acme Will Back First Phase of OpenAI Project With as Much as $105 Billion",
        "Acme pledges $100bn backing for OpenAI data centre in Ohio",
    )
    items = [
        event(
            f"ohio-{index}",
            title=title,
            event_type=EventType.INVESTMENT,
            publisher=publisher,
            hours_old=hours,
        )
        for index, (title, publisher, hours) in enumerate(
            zip(titles, ("Reuters", "Bloomberg", "Financial Times"), (0, 1, 6), strict=True)
        )
    ]

    for first, second in itertools.combinations(titles, 2):
        assert overlap(first, second) < SYNDICATION_OVERLAP

    groups = group_material_events(items)

    assert len(groups) == 1
    assert groups[0].publisher_count == 3


def test_opposed_directions_stay_separate_even_with_a_shared_anchor() -> None:
    """One policy framed two ways is two developments; Stage A already draws that distinction."""

    eased = event(
        "eased",
        title="China eases limits on Acme H200 chips as AI race escalates",
        event_type=EventType.REGULATION,
        direction=EventDirection.POSITIVE,
    )
    banned = event(
        "banned",
        title="China banned Acme H200 chips from export in new rules",
        event_type=EventType.REGULATION,
        direction=EventDirection.NEGATIVE,
        hours_old=6,
    )
    first, second = eased.source_reference.title, banned.source_reference.title

    assert overlap(first, second) < SYNDICATION_OVERLAP
    assert anchor_terms(first, ACME) & anchor_terms(second, ACME)
    assert not describes_same_material_event(eased, banned)
    assert len(group_material_events([eased, banned])) == 2


def test_grouping_closes_transitively_across_the_window() -> None:
    chain = [
        event(
            f"chain-{name}",
            title=NEAR_DUPLICATE_TITLE,
            event_type=EventType.CONTRACT_AWARD,
            publisher=publisher,
            hours_old=hours,
        )
        for name, publisher, hours in (
            ("a", "Reuters", 0),
            ("b", "Bloomberg", 60),
            ("c", "CNBC", 120),
        )
    ]

    groups = group_material_events(chain)

    assert not describes_same_material_event(chain[0], chain[2])
    assert len(groups) == 1
    assert len(groups[0].members) == 3


def test_grouping_is_deterministic_for_one_input_order() -> None:
    items = [
        event("dup-a", title=NEAR_DUPLICATE_TITLE, event_type=EventType.CONTRACT_AWARD),
        event("solo", title="Acme lands a datacentre order", event_type=EventType.CONTRACT_AWARD),
        event(
            "dup-b",
            title=NEAR_DUPLICATE_TITLE,
            event_type=EventType.CONTRACT_AWARD,
            publisher="Bloomberg",
            hours_old=2,
        ),
    ]

    def shape() -> list[tuple[str, ...]]:
        return [
            tuple(item.article_id for item in group.members)
            for group in group_material_events(items)
        ]

    assert shape() == shape()
    assert shape() == [("dup-a", "dup-b"), ("solo",)]


def test_group_primary_is_its_strongest_member() -> None:
    weak = event("weak", title=NEAR_DUPLICATE_TITLE, event_type=EventType.CONTRACT_AWARD)
    strong = event(
        "strong",
        title=NEAR_DUPLICATE_TITLE,
        event_type=EventType.CONTRACT_AWARD,
        magnitude=0.80,
        publisher="Bloomberg",
        hours_old=3,
    )

    groups = group_material_events([weak, strong])

    assert len(groups) == 1
    assert groups[0].primary.article_id == "strong"
    assert groups[0].members[0].article_id == "strong"


def test_magnitude_outranks_event_class_tier() -> None:
    """Tier is a tie-breaker. A large launch must not sit below a small legal footnote."""

    launch = event(
        "big-launch",
        title="Acme unveils PC superchip",
        event_type=EventType.PRODUCT_LAUNCH,
        magnitude=0.80,
    )
    suit = event(
        "small-suit",
        title="Acme sued by Jamendo over AI training data",
        event_type=EventType.LITIGATION,
        direction=EventDirection.NEGATIVE,
    )

    rows = prepare_key_developments([suit, launch]).rows

    assert [row.event.article_id for row in rows] == ["big-launch", "small-suit"]
    assert rows[0].assessment.tier == TIER_PRODUCT_OR_PARTNERSHIP
    assert rows[1].assessment.tier == TIER_DISCLOSURE_OR_LEGAL


def test_tier_then_evidence_breadth_breaks_magnitude_ties() -> None:
    launch = event("launch", title="Acme unveils PC superchip", event_type=EventType.PRODUCT_LAUNCH)
    supported = event(
        "supported-launch",
        title="Acme unveils rack scale platform",
        event_type=EventType.PRODUCT_LAUNCH,
        external_publishers=("Bloomberg",),
    )
    deal = event("deal")

    rows = prepare_key_developments([launch, supported, deal]).rows

    assert [row.event.article_id for row in rows] == ["deal", "supported-launch", "launch"]


def test_publisher_breadth_breaks_ties_between_equally_supported_developments() -> None:
    grouped = [
        event("pair-a", title=NEAR_DUPLICATE_TITLE, event_type=EventType.CONTRACT_AWARD),
        event(
            "pair-b",
            title=NEAR_DUPLICATE_TITLE,
            event_type=EventType.CONTRACT_AWARD,
            publisher="Bloomberg",
            hours_old=1,
        ),
    ]
    solo = event(
        "solo",
        title="Acme lands a datacentre order",
        event_type=EventType.CONTRACT_AWARD,
        publisher="CNBC",
    )

    rows = prepare_key_developments([*grouped, solo]).rows

    assert [row.event.article_id for row in rows] == ["pair-a", "solo"]
    assert rows[0].provenance_note == "2 reports · 2 publishers"
    assert rows[1].provenance_note == "1 report · 1 publisher"


def test_empty_input_and_limits_are_reported_honestly() -> None:
    empty = prepare_key_developments([])

    assert empty.rows == ()
    assert empty.diagnostics.considered == 0
    assert empty.diagnostics.developments == 0

    items = [
        event("deal"),
        event(
            "suit",
            title="Acme sued by Jamendo over AI training data",
            event_type=EventType.LITIGATION,
        ),
        event("order", title="Acme lands a datacentre order", event_type=EventType.CONTRACT_AWARD),
    ]
    limited = prepare_key_developments(items, limit=1)

    assert len(limited.rows) == 1
    assert limited.diagnostics.developments == 3
    assert limited.diagnostics.rendered == 1
    assert prepare_key_developments(items, limit=0).rows == ()


def test_diagnostics_reconcile_considered_against_material_and_every_rejection() -> None:
    items = [
        event("material"),
        event("commentary", title="Why Acme's next quarter could disappoint"),
        event("weak", magnitude=0.10),
        event("untyped", event_type=EventType.OTHER),
        event("fleeting", event_type=EventType.PARTNERSHIP, horizon=TimeHorizon.DAYS),
    ]

    diagnostics = prepare_key_developments(items).diagnostics

    assert diagnostics.considered == len(items)
    assert diagnostics.considered == diagnostics.material + diagnostics.rejected
    assert set(diagnostics.rejected_by_condition) == set(REJECTION_CONDITIONS)
    assert diagnostics.rejected_by_condition[GUARD_COMMENTARY] == 1
    assert diagnostics.rejected_by_condition[DRIVER_NOT_MEANINGFUL] == 1
    assert diagnostics.rejected_by_condition[DRIVER_EVENT_TYPE] == 1
    assert diagnostics.rejected_by_condition[DURABILITY] == 1
