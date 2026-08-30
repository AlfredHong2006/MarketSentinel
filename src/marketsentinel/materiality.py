"""Deterministic materiality assessment, grouping, and ranking for Key Developments.

The question here is narrower than the one Today's Intelligence answers. That surface asks whether
an extraction is concrete and reliable; this one asks whether the development changes an
identifiable driver of the company's future cash flows or risk profile, persists beyond the news
cycle, and is evidenced by a source with direct knowledge or independent corroboration. Market
reaction, commentary, and previews are never material.

The answer is a set of auditable booleans, never a blended score: each rejection names the one
condition that failed, so a surprising verdict can be argued with rather than merely disbelieved.
Nothing is persisted -- groups and verdicts recompute from stored analyses on every request, the
same discipline ``rank_company_risks`` follows, so a policy change needs no migration.

Every rule reuses a primitive the product already agreed on: the shared meaningful-event floor,
the selector's commentary and market-move guards, its disclosure vocabulary, the corroboration
summary, the risk layer's title stemmer, and the shared subject-principal rule the risk layer also
reads. Only one rule is new here -- the percentage price-move guard -- and it is deliberately
local to this module rather than shared with selection, which must keep admitting price-reaction
articles as analysis candidates.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from marketsentinel.analysis_candidates import (
    describes_market_move,
    has_financial_disclosure_signal,
    reads_as_commentary,
)
from marketsentinel.article_sources import source_organization
from marketsentinel.dashboard_intelligence import (
    CorroborationSummary,
    contradiction_label,
    corroboration_metric,
    impact_label,
    primary_source_label,
    summarize_corroboration,
)
from marketsentinel.domain import (
    CompanyIntelligenceEvent,
    CompanyReference,
    EventType,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.event_policy import is_meaningful_event

# ``_stem`` is imported rather than re-implemented: anchor terms must be drawn from exactly the
# vocabulary ``title_terms`` produces, and a second stemmer here would let the two silently drift.
from marketsentinel.risk_scoring import _stem, title_terms
from marketsentinel.subject_principal import reads_as_third_party_appointment

MAX_KEY_DEVELOPMENTS = 8

# Says what was examined and what survived, so an empty or short list reads as a verdict rather
# than as missing data. Absence of material developments is a finding about the coverage.
EMPTY_KEY_DEVELOPMENTS_MESSAGE = (
    "No sufficiently evidenced material developments in the analysed coverage yet."
)

# Every type that names a driver. ``other`` and ``uncertain`` name none by construction, which is
# why they are the only two the driver condition rejects outright -- and why an ``other`` row that
# does name a periodic disclosure or a regulator action is rescued below.
QUALIFYING_EVENT_TYPES = frozenset(EventType) - {EventType.OTHER, EventType.UNCERTAIN}

# Durable by nature whatever Stage A's horizon says. A copyright suit filed today is a multi-year
# exposure even when the extraction hedges its horizon to ``uncertain``, and a quarter of the
# analysed corpus carries that hedge, so a horizon-only durability rule would discard real events.
DURABLE_EVENT_TYPES = frozenset(
    {
        EventType.EARNINGS,
        EventType.ACQUISITION,
        EventType.REGULATION,
        EventType.LITIGATION,
        EventType.ANALYST_OR_GUIDANCE_CHANGE,
        EventType.MANAGEMENT_CHANGE,
        EventType.CONTRACT_AWARD,
        EventType.CONTRACT_LOSS,
    }
)
DURABLE_HORIZONS = frozenset({TimeHorizon.WEEKS, TimeHorizon.MONTHS, TimeHorizon.LONG_TERM})

# Types whose supply is dominated by issuer editorial. A newsroom post announcing a product or a
# partnership is a statement of intent, not evidence that a driver moved, so these are the types
# an official primary may not carry on its own voice alone.
EDITORIAL_PRONE_EVENT_TYPES = frozenset(
    {
        EventType.PRODUCT_LAUNCH,
        EventType.PARTNERSHIP,
        EventType.OTHER,
    }
)

# A filing or a major financial wire is a record of the event in its own right. Corroboration
# cannot be a universal requirement: roughly two thirds of analysed articles carry none at all,
# and requiring it would reject genuine disclosures for being reported once.
EVIDENCED_SOURCE_CLASSES = frozenset(
    {
        SourceClass.REGULATORY_OR_FILING,
        SourceClass.MAJOR_FINANCIAL_NEWS,
    }
)

TIER_DISCLOSURE_OR_LEGAL = 1
TIER_CAPITAL_OR_OPERATIONS = 2
TIER_PRODUCT_OR_PARTNERSHIP = 3

# Tier orders event classes by how directly they bear on a driver, and is only ever a tie-breaker
# below magnitude: a large product launch must still outrank a small regulatory footnote.
EVENT_CLASS_TIERS: Mapping[EventType, int] = {
    EventType.EARNINGS: TIER_DISCLOSURE_OR_LEGAL,
    EventType.REGULATION: TIER_DISCLOSURE_OR_LEGAL,
    EventType.LITIGATION: TIER_DISCLOSURE_OR_LEGAL,
    EventType.ACQUISITION: TIER_DISCLOSURE_OR_LEGAL,
    EventType.ANALYST_OR_GUIDANCE_CHANGE: TIER_DISCLOSURE_OR_LEGAL,
    EventType.INVESTMENT: TIER_CAPITAL_OR_OPERATIONS,
    EventType.FINANCING: TIER_CAPITAL_OR_OPERATIONS,
    EventType.SUPPLY_DISRUPTION: TIER_CAPITAL_OR_OPERATIONS,
    EventType.MANAGEMENT_CHANGE: TIER_CAPITAL_OR_OPERATIONS,
    EventType.CONTRACT_AWARD: TIER_CAPITAL_OR_OPERATIONS,
    EventType.CONTRACT_LOSS: TIER_CAPITAL_OR_OPERATIONS,
    EventType.MACROECONOMIC_EXPOSURE: TIER_CAPITAL_OR_OPERATIONS,
    EventType.PRODUCT_LAUNCH: TIER_PRODUCT_OR_PARTNERSHIP,
    EventType.PARTNERSHIP: TIER_PRODUCT_OR_PARTNERSHIP,
}

TIER_LABELS: Mapping[int, str] = {
    TIER_DISCLOSURE_OR_LEGAL: "Results, deal, or legal and regulatory",
    TIER_CAPITAL_OR_OPERATIONS: "Capital, supply, or contract",
    TIER_PRODUCT_OR_PARTNERSHIP: "Product or partnership",
}

# A bound on how far apart two reports of one story may sit, not a claim about how long a story
# runs. Every same-story pair observed in the analysed corpus lands within six hours; the window
# is deliberate headroom for slower republication, so it is never itself the reason one event
# renders as two rows.
MATERIAL_GROUPING_WINDOW = timedelta(hours=96)
SYNDICATION_OVERLAP = 0.75
SHARED_STORY_OVERLAP = 0.30
MIN_ANCHOR_LENGTH = 4

GUARD_COMMENTARY = "guard:commentary"
GUARD_MARKET_MOVE = "guard:market_move"
GUARD_PRICE_MOVE = "guard:price_move"
GUARD_THIRD_PARTY_APPOINTMENT = "guard:third_party_appointment"
DRIVER_NOT_MEANINGFUL = "driver:not_meaningful"
DRIVER_EVENT_TYPE = "driver:event_type"
DURABILITY = "durability"
EVIDENCE = "evidence"

REJECTION_CONDITIONS = (
    GUARD_COMMENTARY,
    GUARD_MARKET_MOVE,
    GUARD_PRICE_MOVE,
    GUARD_THIRD_PARTY_APPOINTMENT,
    DRIVER_NOT_MEANINGFUL,
    DRIVER_EVENT_TYPE,
    DURABILITY,
    EVIDENCE,
)

# Price-reaction headlines that name no instrument ("Supermicro drops 33% after ...") slip past the
# selector's market-move guard, which keys on shares/stock/market-cap wording. This one keys on the
# move itself. It is knowingly blunt: a subject-share gain phrased as a move verb plus a percentage
# ("gains 5% of the market") would also be rejected. No such row appears in the analysed corpus,
# and narrowing the verb list to exclude it would risk over-fitting to a case that has not occurred.
_PRICE_MOVE_VERBS = (
    r"climb|crash|dive|drop|fall|fell|gain|jump|lose|loses|lost|plunge|rall|rise|rose"
    r"|sank|shed|sink|slid|slide|slip|slump|soar|spike|surge|tumble|sell[- ]?off"
)
# The same verbs describe a reported metric moving ("revenue jumps 40%"), which is the disclosure
# itself rather than a reaction to one -- and is the commonest shape an earnings headline takes.
# The noun immediately before the verb is what separates the two, so it is captured and vetoed
# here rather than removing verbs the price-reaction reading needs.
_PERCENT_PRICE_MOVE_PATTERN = re.compile(
    rf"(?:(?P<subject>\w+)\s+)?\b(?:{_PRICE_MOVE_VERBS})\w*\s+"
    rf"(?:\w+\s+){{0,2}}?(?:by\s+)?~?\d+(?:\.\d+)?\s*(?:%|percent\b)",
    re.I,
)
# ``percent\b`` above keeps "percentage points" out; this keeps the metric itself out.
_METRIC_SUBJECT_PATTERN = re.compile(
    r"revenues?|profits?|sales|earnings|income|margins?|deliveries|shipments|bookings|output"
    r"|production|eps",
    re.I,
)

# Anchors are read from raw tokens, before casefolding, because capitalisation is the signal that
# a token names something rather than describes it.
_RAW_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9$][\w$.-]*")
_SUBJECT_WORD_PATTERN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class MaterialityAssessment:
    """One auditable verdict: whether an event is material, and which condition decided it."""

    material: bool
    failed_condition: str | None
    reasons: tuple[str, ...]
    passes_guard: bool
    passes_driver: bool
    passes_durability: bool
    passes_evidence: bool
    rescued_disclosure: bool
    tier: int | None
    corroboration: CorroborationSummary


@dataclass(frozen=True)
class MaterialEventGroup:
    """Reports of one underlying development, represented by their strongest member."""

    primary: CompanyIntelligenceEvent
    primary_assessment: MaterialityAssessment
    members: tuple[CompanyIntelligenceEvent, ...]
    publisher_count: int


@dataclass(frozen=True)
class KeyDevelopmentRow:
    """A display-ready material development backed only by compatible stored analyses."""

    event: CompanyIntelligenceEvent
    assessment: MaterialityAssessment
    group: MaterialEventGroup
    impact_label: str
    tier_label: str
    corroboration: CorroborationSummary
    corroboration_metric: str
    contradiction_label: str | None
    primary_source_label: str
    provenance_note: str


@dataclass(frozen=True)
class MaterialityDiagnostics:
    """Deterministic counters for one Key Developments computation.

    ``considered`` always reconciles: it equals ``material`` plus every recorded rejection.
    ``rendered`` is stated separately from ``developments`` so a limit never reads as an absence.
    """

    considered: int
    material: int
    developments: int
    rendered: int
    rejected_by_condition: Mapping[str, int]

    @property
    def rejected(self) -> int:
        return sum(self.rejected_by_condition.values())


@dataclass(frozen=True)
class KeyDevelopments:
    rows: tuple[KeyDevelopmentRow, ...]
    diagnostics: MaterialityDiagnostics


def describes_percent_price_move(title: str) -> bool:
    """Whether a title's subject is a percentage price move rather than a company event.

    Every match is examined, not just the first: a title may report a metric and a price reaction
    in one line, and the reaction is what decides it.
    """

    for match in _PERCENT_PRICE_MOVE_PATTERN.finditer(title):
        subject = match.group("subject")
        if subject is None or not _METRIC_SUBJECT_PATTERN.fullmatch(subject):
            return True
    return False


def event_class_tier(event_type: EventType, *, rescued_disclosure: bool = False) -> int:
    """Rank an event class by how directly it bears on a cash-flow or risk driver.

    A rescued ``other`` is tier 1 because the vocabulary that rescued it *is* the disclosure and
    regulator-action vocabulary. An untyped event otherwise ranks last; the gate admits none.
    """

    if rescued_disclosure:
        return TIER_DISCLOSURE_OR_LEGAL
    return EVENT_CLASS_TIERS.get(event_type, TIER_PRODUCT_OR_PARTNERSHIP)


def assess_materiality(event: CompanyIntelligenceEvent) -> MaterialityAssessment:
    """Apply the four materiality conditions in order and record the first one that fails.

    A contradicted claim never rejects a row. Conflict is reported on the development itself, so a
    disputed event stays visible with its dispute attached rather than disappearing silently.
    """

    title = event.source_reference.title
    event_type = event.event.event_type
    corroboration = summarize_corroboration(event)
    # Resolved up front because both the driver and durability conditions consult it: an ``other``
    # row naming a periodic disclosure or a regulator action is a real driver that Stage A only
    # failed to type. The rescue reads the *disclosure* vocabulary rather than the broader priority
    # signal, which also matches advocacy about an action ("senators say exports must be suspended").
    rescued = event_type is EventType.OTHER and has_financial_disclosure_signal(title)

    guard = _failed_guard(event)
    if guard is not None:
        condition, reason = guard
        return _rejection(condition, reason, corroboration, rescued, passes_guard=False)

    if not is_meaningful_event(event.event):
        return _rejection(
            DRIVER_NOT_MEANINGFUL,
            "Below the shared floor for a concrete, sufficiently confident event",
            corroboration,
            rescued,
        )
    if event_type not in QUALIFYING_EVENT_TYPES and not rescued:
        return _rejection(
            DRIVER_EVENT_TYPE,
            "Event type names no identifiable cash-flow or risk driver",
            corroboration,
            rescued,
        )

    durable = (
        event_type in DURABLE_EVENT_TYPES or rescued or event.event.time_horizon in DURABLE_HORIZONS
    )
    if not durable:
        return _rejection(
            DURABILITY,
            "Neither an inherently durable event type nor a horizon beyond the news cycle",
            corroboration,
            rescued,
            passes_driver=True,
        )

    if not _is_evidenced(event, corroboration, rescued_disclosure=rescued):
        return _rejection(
            EVIDENCE,
            "No qualifying source class, external support, or first-hand disclosure",
            corroboration,
            rescued,
            passes_driver=True,
            passes_durability=True,
        )

    return MaterialityAssessment(
        material=True,
        failed_condition=None,
        reasons=(),
        passes_guard=True,
        passes_driver=True,
        passes_durability=True,
        passes_evidence=True,
        rescued_disclosure=rescued,
        tier=event_class_tier(event_type, rescued_disclosure=rescued),
        corroboration=corroboration,
    )


def is_material(event: CompanyIntelligenceEvent) -> bool:
    """Whether one stored event is a material development for a medium-term investor."""

    return assess_materiality(event).material


def anchor_terms(title: str, subject: CompanyReference) -> frozenset[str]:
    """Distinctive stemmed terms a title uses to name *which* event it reports.

    A shared anchor is what separates two reports of one deal from two reports of different deals
    of the same shape. Only capitalised or digit-bearing raw tokens qualify -- names, places, and
    amounts -- and the subject company is removed, since every headline about it names it and it
    would otherwise anchor every pair to every other.
    """

    ignored = _subject_terms(subject)
    anchors = set()
    for token in _RAW_TOKEN_PATTERN.findall(title):
        if not (token[0].isupper() or any(character.isdigit() for character in token)):
            continue
        term = _stem(re.sub(r"[^a-z0-9]", "", token.casefold()))
        if len(term) >= MIN_ANCHOR_LENGTH and term not in ignored:
            anchors.add(term)
    return frozenset(anchors)


def describes_same_material_event(
    first: CompanyIntelligenceEvent,
    second: CompanyIntelligenceEvent,
) -> bool:
    """Whether two material events are two reports of one underlying development.

    Two arms, because one story reaches the corpus in two distinguishable shapes. A syndicated or
    near-duplicate title needs no further evidence: heavy wording overlap is the whole signal, and
    unlike the risk layer's rule no distinctive-term veto applies, since byte-identical low-signal
    titles must not be split by the one word that happens to differ. A story told independently by
    several outlets shares far less wording, so it must agree on what happened and to whom -- same
    event type, same direction, and at least one shared anchor naming the same subject matter.
    """

    published_gap = abs(first.source_reference.published_at - second.source_reference.published_at)
    if published_gap > MATERIAL_GROUPING_WINDOW:
        return False
    left = title_terms(first.source_reference.title)
    right = title_terms(second.source_reference.title)
    if not left or not right:
        return False
    overlap = len(left & right) / min(len(left), len(right))
    if overlap >= SYNDICATION_OVERLAP:
        return True
    if first.event.event_type is not second.event.event_type:
        return False
    if first.event.direction is not second.event.direction:
        return False
    if overlap < SHARED_STORY_OVERLAP:
        return False
    return bool(
        anchor_terms(first.source_reference.title, first.subject_company)
        & anchor_terms(second.source_reference.title, second.subject_company)
    )


def group_material_events(
    events: Sequence[CompanyIntelligenceEvent],
) -> list[MaterialEventGroup]:
    """Group the material events among ``events`` by transitive same-development similarity."""

    return _group_assessed([(event, assess_materiality(event)) for event in events])


def prepare_key_developments(
    events: Sequence[CompanyIntelligenceEvent],
    *,
    limit: int = MAX_KEY_DEVELOPMENTS,
) -> KeyDevelopments:
    """Return the material developments in one company's stored coverage, strongest first.

    The order is lexicographic and never blended. Magnitude leads, so the largest disclosed
    commitments cannot be displaced by a smaller event of a more serious class; tier then breaks
    the magnitude ties that dominate a real corpus, and evidence breadth breaks the rest.
    """

    assessed = [(event, assess_materiality(event)) for event in events]
    groups = sorted(_group_assessed(assessed), key=_group_sort_key)
    rendered = groups[: max(limit, 0)]
    rejected = dict.fromkeys(REJECTION_CONDITIONS, 0)
    for _, assessment in assessed:
        if assessment.failed_condition is not None:
            rejected[assessment.failed_condition] += 1
    return KeyDevelopments(
        rows=tuple(_key_development_row(group) for group in rendered),
        diagnostics=MaterialityDiagnostics(
            considered=len(assessed),
            material=sum(1 for _, assessment in assessed if assessment.material),
            developments=len(groups),
            rendered=len(rendered),
            rejected_by_condition=rejected,
        ),
    )


def key_developments_caption(diagnostics: MaterialityDiagnostics) -> str:
    """State the funnel behind the rendered list: what was examined, what passed, what merged.

    Both narrowings are named because each is a different claim. Analysed to material is the
    gate's verdict; material to developments is grouping folding several reports of one event into
    one row. A truncated list says so explicitly, so a limit is never read as an absence.
    """

    caption = (
        f"{diagnostics.considered} analysed → {diagnostics.material} material"
        f" → {diagnostics.developments} developments"
    )
    if diagnostics.rendered < diagnostics.developments:
        return f"{caption} · showing the strongest {diagnostics.rendered}"
    return caption


_GUARD_CONDITIONS = (
    (
        GUARD_COMMENTARY,
        reads_as_commentary,
        "Frames itself as explanation, advocacy, or a preview rather than a report",
    ),
    (
        GUARD_MARKET_MOVE,
        describes_market_move,
        "Reports a share-price or market-value move rather than a company event",
    ),
    (
        GUARD_PRICE_MOVE,
        describes_percent_price_move,
        "Reports a percentage price move rather than a company event",
    ),
)


def _failed_guard(event: CompanyIntelligenceEvent) -> tuple[str, str] | None:
    """Reject on framing first, then on whose event it is.

    The subject check runs last and separately because it is the only guard that needs to know
    which company is under analysis: the three above ask what kind of article this is, this one
    asks whether the company is a party to what the article reports.
    """

    title = event.source_reference.title
    for condition, describes, reason in _GUARD_CONDITIONS:
        if describes(title):
            return condition, reason
    if reads_as_third_party_appointment(title, event.subject_company):
        return (
            GUARD_THIRD_PARTY_APPOINTMENT,
            "Reports another organisation appointing a company executive, not a company event",
        )
    return None


def _is_evidenced(
    event: CompanyIntelligenceEvent,
    corroboration: CorroborationSummary,
    *,
    rescued_disclosure: bool,
) -> bool:
    """Whether anything establishes the development beyond a single interested assertion.

    An official primary is first-hand knowledge for what the company is doing with its own money
    or its own obligations, so a company-announced investment or disclosure stands alone. It is
    not evidence for the editorial-prone classes, where the announcement is the product.
    """

    if event.source_class in EVIDENCED_SOURCE_CLASSES or corroboration.has_external_support:
        return True
    if event.source_class is not SourceClass.OFFICIAL_COMPANY:
        return False
    return (
        event.event.event_type not in EDITORIAL_PRONE_EVENT_TYPES
        or rescued_disclosure
        or has_financial_disclosure_signal(event.source_reference.title)
    )


def _rejection(
    condition: str,
    reason: str,
    corroboration: CorroborationSummary,
    rescued_disclosure: bool,
    *,
    passes_guard: bool = True,
    passes_driver: bool = False,
    passes_durability: bool = False,
) -> MaterialityAssessment:
    return MaterialityAssessment(
        material=False,
        failed_condition=condition,
        reasons=(reason,),
        passes_guard=passes_guard,
        passes_driver=passes_driver,
        passes_durability=passes_durability,
        passes_evidence=False,
        rescued_disclosure=rescued_disclosure,
        tier=None,
        corroboration=corroboration,
    )


def _subject_terms(subject: CompanyReference) -> frozenset[str]:
    words = _SUBJECT_WORD_PATTERN.findall(f"{subject.name} {subject.symbol}".casefold())
    return frozenset(_stem(word) for word in words)


_Assessed = tuple[CompanyIntelligenceEvent, MaterialityAssessment]


def _group_assessed(assessed: Sequence[_Assessed]) -> list[MaterialEventGroup]:
    """Transitive closure in input order, mirroring the risk layer's grouping idiom.

    Every group the item matches is folded into the first of them, not only the first one found.
    Two reports of one development can each match a third that arrives later while matching
    neither the other directly, and stopping at the first match would leave that development
    rendered as two rows whose report and publisher counts each understate its breadth.
    """

    groups: list[list[_Assessed]] = []
    for item in assessed:
        if not item[1].material:
            continue
        matched = [
            group
            for group in groups
            if any(describes_same_material_event(item[0], member) for member, _ in group)
        ]
        if not matched:
            groups.append([item])
            continue
        first, *rest = matched
        first.append(item)
        for group in rest:
            first.extend(group)
            groups.remove(group)
    return [_material_group(group) for group in groups]


def _material_group(group: Sequence[_Assessed]) -> MaterialEventGroup:
    ordered = sorted(group, key=lambda item: _member_sort_key(*item))
    primary, primary_assessment = ordered[0]
    return MaterialEventGroup(
        primary=primary,
        primary_assessment=primary_assessment,
        members=tuple(event for event, _ in ordered),
        publisher_count=len({_organization(event) for event, _ in ordered}),
    )


def _member_sort_key(
    event: CompanyIntelligenceEvent,
    assessment: MaterialityAssessment,
) -> tuple[float, int, int, float, str]:
    # Timestamps are negated rather than reversed because the key mixes ascending tier with
    # descending magnitude, evidence, and recency in one lexicographic comparison.
    return (
        -event.event.magnitude,
        assessment.tier or TIER_PRODUCT_OR_PARTNERSHIP,
        -assessment.corroboration.external_sources,
        -event.source_reference.published_at.timestamp(),
        event.article_id,
    )


def _group_sort_key(group: MaterialEventGroup) -> tuple[float, int, int, int, float, str]:
    event, assessment = group.primary, group.primary_assessment
    return (
        -event.event.magnitude,
        assessment.tier or TIER_PRODUCT_OR_PARTNERSHIP,
        -assessment.corroboration.external_sources,
        -group.publisher_count,
        -event.source_reference.published_at.timestamp(),
        event.article_id,
    )


def _key_development_row(group: MaterialEventGroup) -> KeyDevelopmentRow:
    assessment = group.primary_assessment
    summary = assessment.corroboration
    return KeyDevelopmentRow(
        event=group.primary,
        assessment=assessment,
        group=group,
        impact_label=impact_label(group.primary.event.magnitude),
        tier_label=TIER_LABELS[assessment.tier or TIER_PRODUCT_OR_PARTNERSHIP],
        corroboration=summary,
        corroboration_metric=corroboration_metric(summary),
        contradiction_label=contradiction_label(summary),
        primary_source_label=primary_source_label(group.primary.source_class),
        provenance_note=_provenance_note(group),
    )


def _provenance_note(group: MaterialEventGroup) -> str:
    reports = _plural(len(group.members), "report", "reports")
    publishers = _plural(group.publisher_count, "publisher", "publishers")
    return f"{reports} · {publishers}"


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _organization(event: CompanyIntelligenceEvent) -> str:
    reference = event.source_reference
    return source_organization(reference.publisher, reference.url, reference.title)
