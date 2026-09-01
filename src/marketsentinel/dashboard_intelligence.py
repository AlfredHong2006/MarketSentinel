"""Pure eligibility and presentation preparation for Today's Intelligence.

Evidence wording here follows one rule: never describe the supplied comparison pool as support.
Stage B is handed a fixed-size set of comparison articles and returns a verdict per claim, so
pool size says only how much material was examined. What may be reported as support is the set
of articles a corroborated claim actually cited, reduced to distinct publishing organisations
and excluding the primary's own voice. Those are called *external* sources rather than
independent ones: the exclusions below remove the obvious self-references, but nothing in the
stored data establishes editorial or ownership independence between the remaining publishers.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from marketsentinel.article_sources import source_organization
from marketsentinel.domain import (
    ArticleEvidenceReference,
    CompanyIntelligenceEvent,
    EvidenceStatus,
    SourceClass,
)
from marketsentinel.event_policy import is_meaningful_event

MAX_TODAYS_INTELLIGENCE = 4

# Shared verbatim with the API's Today's Intelligence projection (overview.py) so the Streamlit
# and React clients state the exact same ranking rule and absence message.
TODAYS_INTELLIGENCE_CAPTION = (
    "Ordered by event magnitude, extraction confidence, evidence strength, source class, "
    "and publication time. These are event assessments, not price predictions."
)
EMPTY_TODAYS_INTELLIGENCE_MESSAGE = "No high-confidence analysed events available yet."

# A syndicated rewrite of the primary is the same reporting, not a second look at the event.
# These mirror analysis_candidates' near-title rule so both sides of the product agree on what
# counts as one story.
NEAR_TITLE_OVERLAP = 0.75
NEAR_TITLE_WINDOW = timedelta(hours=48)
_TITLE_IGNORED = {"with", "from", "that", "this", "will", "after", "about", "the", "and", "for"}

_PRIMARY_SOURCE_NAMES = {
    SourceClass.REGULATORY_OR_FILING: "Regulatory filing",
    SourceClass.OFFICIAL_COMPANY: "Company statement",
    SourceClass.MAJOR_FINANCIAL_NEWS: "Major financial news",
    SourceClass.INDUSTRY_SPECIALIST: "Industry specialist",
    SourceClass.GENERAL_NEWS: "General news",
    SourceClass.COMMENTARY_OR_OPINION: "Commentary or opinion",
    SourceClass.UNKNOWN: "Unknown source",
}

_SOURCE_QUALITY = {
    SourceClass.REGULATORY_OR_FILING: 6,
    SourceClass.OFFICIAL_COMPANY: 5,
    SourceClass.MAJOR_FINANCIAL_NEWS: 4,
    SourceClass.INDUSTRY_SPECIALIST: 3,
    SourceClass.GENERAL_NEWS: 2,
    SourceClass.COMMENTARY_OR_OPINION: 1,
    SourceClass.UNKNOWN: 0,
}


@dataclass(frozen=True)
class CorroborationSummary:
    """What the stored Stage B verdicts actually establish about one analysis.

    ``comparison_articles`` is how much material was examined and is never support.
    ``external_sources`` counts distinct publishing organisations, other than the primary's own,
    whose articles a corroborated claim cited.
    """

    total_claims: int
    corroborated_claims: int
    contradicted_claims: int
    unresolved_claims: int
    comparison_articles: int
    supporting_articles: int
    external_sources: int
    primary_is_official: bool

    @property
    def has_corroboration(self) -> bool:
        return self.corroborated_claims > 0

    @property
    def has_external_support(self) -> bool:
        return self.external_sources > 0

    @property
    def has_contradiction(self) -> bool:
        return self.contradicted_claims > 0


@dataclass(frozen=True)
class IntelligenceCard:
    """A display-ready view backed only by a compatible stored analysis."""

    event: CompanyIntelligenceEvent
    impact_label: str
    impact_score: int
    corroboration: CorroborationSummary
    corroboration_label: str
    corroboration_metric: str
    contradiction_label: str | None
    primary_source_label: str


def compatible_intelligence_events(
    payload: Sequence[Mapping[str, Any]],
) -> list[CompanyIntelligenceEvent]:
    """Reject incompatible stored/API records instead of inventing missing fields."""

    compatible: list[CompanyIntelligenceEvent] = []
    for item in payload:
        try:
            compatible.append(CompanyIntelligenceEvent.model_validate_json(json.dumps(item)))
        except ValidationError:
            continue
    return compatible


def is_intelligence_eligible(event: CompanyIntelligenceEvent) -> bool:
    """Keep the main dashboard focused on concrete, well-supported events."""

    return is_meaningful_event(event.event)


def prepare_todays_intelligence(
    events: Sequence[CompanyIntelligenceEvent],
    *,
    limit: int = MAX_TODAYS_INTELLIGENCE,
) -> list[IntelligenceCard]:
    """Return the most material stored event cards in a deterministic order.

    The ranking is deliberately transparent: event magnitude, extraction confidence,
    deterministic evidence strength, source class, then publication time.
    """

    # evidence_sources is the same list the backend counts as evidence_count (event_analysis.py);
    # CompanyIntelligenceEvent just doesn't carry evidence_count separately, so len() here is exact.
    eligible = [item for item in events if is_intelligence_eligible(item)]
    eligible.sort(
        key=lambda item: (
            item.event.magnitude,
            item.event.model_confidence,
            item.evidence_strength,
            _SOURCE_QUALITY[item.source_class],
            item.source_reference.published_at,
        ),
        reverse=True,
    )
    return [prepare_intelligence_card(item) for item in eligible[:limit]]


def prepare_intelligence_card(item: CompanyIntelligenceEvent) -> IntelligenceCard:
    """Build the display-ready view of one stored analysis, ranked or not.

    Public because the same labels and the same corroboration semantics must describe an analysis
    opened from the article browser as describe one that reached the top of the ranking. Reading a
    stored analysis directly must never produce a second opinion about its evidence.
    """

    summary = summarize_corroboration(item)
    return IntelligenceCard(
        event=item,
        impact_label=impact_label(item.event.magnitude),
        impact_score=round(item.event.magnitude * 100),
        corroboration=summary,
        corroboration_label=corroboration_label(summary),
        corroboration_metric=corroboration_metric(summary),
        contradiction_label=contradiction_label(summary),
        primary_source_label=primary_source_label(item.source_class),
    )


def impact_label(magnitude: float) -> str:
    if magnitude >= 0.80:
        return "Very high impact"
    if magnitude >= 0.55:
        return "High impact"
    return "Meaningful impact"


def primary_source_label(source_class: SourceClass) -> str:
    """Name what kind of record the primary is, separately from whether anything corroborates it.

    A filing is an authoritative record of what was filed even with nothing else supporting it,
    so this stays independent of the corroboration wording rather than blended into one score.
    """

    return _PRIMARY_SOURCE_NAMES[source_class]


def summarize_corroboration(event: CompanyIntelligenceEvent) -> CorroborationSummary:
    """Derive honest corroboration counts from stored Stage B verdicts.

    Only articles cited by a corroborated claim can count, and they are then reduced to distinct
    publishing organisations with the primary's own organisation and syndicated rewrites of the
    primary removed, so one story reported twice is not read as two sources.
    """

    by_id = {item.article_id: item for item in event.evidence_sources}
    corroborated = [
        claim
        for claim in event.claims
        if claim.status is EvidenceStatus.CORROBORATED and claim.evidence_article_ids
    ]
    contradicted = [
        claim
        for claim in event.claims
        if claim.status is EvidenceStatus.CONTRADICTED and claim.evidence_article_ids
    ]
    supporting_ids = {
        article_id for claim in corroborated for article_id in claim.evidence_article_ids
    }
    supporting = [by_id[article_id] for article_id in sorted(supporting_ids) if article_id in by_id]
    primary = event.source_reference
    primary_organization = source_organization(primary.publisher, primary.url, primary.title)
    external = {
        source_organization(item.publisher, item.url, item.title)
        for item in supporting
        if source_organization(item.publisher, item.url, item.title) != primary_organization
        and not _restates_primary(primary, item)
    }
    return CorroborationSummary(
        total_claims=len(event.claims),
        corroborated_claims=len(corroborated),
        contradicted_claims=len(contradicted),
        unresolved_claims=len(event.claims) - len(corroborated) - len(contradicted),
        comparison_articles=len(event.evidence_sources),
        supporting_articles=len(supporting),
        external_sources=len(external),
        primary_is_official=event.source_class is SourceClass.OFFICIAL_COMPANY,
    )


def corroboration_metric(summary: CorroborationSummary) -> str:
    """Short headline value; never states a source count the citations do not support."""

    if not summary.has_external_support:
        return "None found"
    if summary.external_sources == 1:
        return "1 external source"
    return f"{summary.external_sources} external sources"


def corroboration_label(summary: CorroborationSummary) -> str:
    """Describe corroboration in full. Absence of support never implies a claim is false."""

    if not summary.has_corroboration:
        return "No external corroboration found"
    if not summary.has_external_support:
        if summary.primary_is_official:
            return "Supported only by the company's own channels"
        return "Supported only by the same publisher"
    claims = _plural(summary.corroborated_claims, "claim corroborated", "claims corroborated")
    sources = _plural(summary.external_sources, "external source", "external sources")
    return f"{claims} · {sources}"


def contradiction_label(summary: CorroborationSummary) -> str | None:
    """Surface conflict whenever it exists, regardless of how strong the primary source is."""

    if not summary.has_contradiction:
        return None
    return f"Conflicting evidence on {summary.contradicted_claims} of {summary.total_claims} claims"


def evidence_breakdown_label(summary: CorroborationSummary) -> str:
    """Expanded-view detail that keeps examined material and actual support clearly apart."""

    examined = _plural(
        summary.comparison_articles, "comparison article evaluated", "comparison articles evaluated"
    )
    return (
        f"{examined} · {summary.corroborated_claims} of {summary.total_claims} claims corroborated"
        f" · {summary.contradicted_claims} contradicted"
        f" · {summary.unresolved_claims} unsupported or uncertain"
    )


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _restates_primary(primary: ArticleEvidenceReference, item: ArticleEvidenceReference) -> bool:
    if abs((primary.published_at - item.published_at).total_seconds()) > (
        NEAR_TITLE_WINDOW.total_seconds()
    ):
        return False
    primary_terms = _title_terms(primary.title)
    item_terms = _title_terms(item.title)
    if not primary_terms or not item_terms:
        return False
    shared = primary_terms & item_terms
    return len(shared) / min(len(primary_terms), len(item_terms)) >= NEAR_TITLE_OVERLAP


def _title_terms(title: str) -> set[str]:
    return {
        term for term in re.findall(r"[a-z0-9]{3,}", title.casefold()) if term not in _TITLE_IGNORED
    }
