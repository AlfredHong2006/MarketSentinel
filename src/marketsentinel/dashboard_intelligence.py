"""Pure eligibility and presentation preparation for Today's Intelligence."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from marketsentinel.domain import CompanyIntelligenceEvent, SourceClass
from marketsentinel.event_policy import is_meaningful_event

MAX_TODAYS_INTELLIGENCE = 4

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
class IntelligenceCard:
    """A display-ready view backed only by a compatible stored analysis."""

    event: CompanyIntelligenceEvent
    impact_label: str
    impact_score: int
    evidence_label: str
    source_quality_label: str
    corroboration_count: int
    corroboration_label: str


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
    return [
        IntelligenceCard(
            event=item,
            impact_label=impact_label(item.event.magnitude),
            impact_score=round(item.event.magnitude * 100),
            evidence_label=evidence_label(item.evidence_strength),
            source_quality_label=source_quality_label(item.source_class),
            corroboration_count=len(item.evidence_sources),
            corroboration_label=corroboration_label(len(item.evidence_sources)),
        )
        for item in eligible[:limit]
    ]


def impact_label(magnitude: float) -> str:
    if magnitude >= 0.80:
        return "Very high impact"
    if magnitude >= 0.55:
        return "High impact"
    return "Meaningful impact"


def evidence_label(strength: float | None) -> str | None:
    """Describe evidence quality; ``None`` remains unavailable rather than becoming zero."""

    if strength is None:
        return None
    if strength >= 0.70:
        return "Strong evidence"
    if strength >= 0.40:
        return "Moderate evidence"
    return "Limited evidence"


def source_quality_label(source_class: SourceClass) -> str:
    return source_class.value.replace("_", " ").title()


def corroboration_label(count: int) -> str:
    """Describe supplied independent source count. Absence never implies a claim is false."""

    if count <= 0:
        return "No additional supplied sources"
    if count == 1:
        return "1 supporting source"
    return f"{count} supporting sources"
