"""Extraction of deterministic downside risk signals from stored article analyses.

Nothing here calls a model, a clock, or a database. A signal exists only because a stored
analysis passed the shared meaningful-event policy, reports something the subject company is a
party to, and either described a realized negative event with a safe theme mapping, or stated an
explicit negative transmission channel. Downside is never inferred from magnitude, from an event
being positive, from share-price reaction, or from source prominence.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from marketsentinel.domain import (
    ArticleAnalysis,
    EventDirection,
    EvidenceStatus,
    RiskDiagnostics,
    RiskTheme,
    SourceClass,
    TimeHorizon,
)
from marketsentinel.event_policy import is_meaningful_event
from marketsentinel.risk_taxonomy import (
    theme_for_mechanism,
    theme_for_realized_negative_event,
)
from marketsentinel.subject_principal import reads_as_third_party_appointment
from marketsentinel.timeutils import ensure_utc

# Realization weighting. A realized negative event is the risk; a stated mechanism on a
# non-negative event is a prospective consequence and is damped accordingly.
REALIZED_NEGATIVE_EVENT_FACTOR = 1.00
NEGATIVE_CHANNEL_FACTORS: dict[EventDirection, float] = {
    EventDirection.NEGATIVE: 0.85,
    EventDirection.MIXED: 0.70,
    EventDirection.POSITIVE: 0.60,
    EventDirection.NEUTRAL: 0.60,
}

# ``evidence_strength`` already adds this much when a claim is corroborated with citations.
# It is removed before the claim adjustment below so corroboration is never counted twice.
EMBEDDED_CORROBORATION_BONUS = 0.15

CLAIM_ADJUSTMENTS: dict[str, float] = {
    "corroborated": 0.10,
    "neutral": 0.00,
    "contradicted": -0.15,
}

SUPPORT_CONFIDENCE_WEIGHT = 0.55
SUPPORT_CONTEXT_WEIGHT = 0.45

HALF_LIFE_DAYS: dict[TimeHorizon, float] = {
    TimeHorizon.IMMEDIATE: 1.0,
    TimeHorizon.DAYS: 7.0,
    TimeHorizon.WEEKS: 28.0,
    TimeHorizon.MONTHS: 120.0,
    TimeHorizon.LONG_TERM: 365.0,
    TimeHorizon.UNCERTAIN: 30.0,
}


class RiskSignalBasis(StrEnum):
    """Why this signal exists at all."""

    REALIZED_EVENT = "realized_event"
    NEGATIVE_CHANNEL = "negative_channel"


@dataclass(frozen=True)
class RiskSignal:
    """One inspectable downside observation traceable to a single stored analysis."""

    theme: RiskTheme
    article_id: str
    publisher: str
    title: str
    url: str
    published_at: datetime
    source_class: SourceClass
    basis: RiskSignalBasis
    mechanism: str
    magnitude: float
    severity: float
    support: float
    decay: float
    score: float


@dataclass(frozen=True)
class RiskSignalExtraction:
    signals: tuple[RiskSignal, ...]
    diagnostics: RiskDiagnostics


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def claim_state(analysis: ArticleAnalysis) -> str:
    """Summarise claim evidence by precedence, never by averaging a confidence value.

    ``unsupported`` and ``uncertain`` mean the supplied evidence did not substantiate a claim.
    That is not counter-evidence, so both are neutral. ``claim.confidence`` is deliberately
    unused: it is self-reported and not a calibrated probability.
    """

    cited = [item for item in analysis.claims if item.evidence_article_ids]
    if any(item.status is EvidenceStatus.CONTRADICTED for item in cited):
        return "contradicted"
    if any(item.status is EvidenceStatus.CORROBORATED for item in cited):
        return "corroborated"
    return "neutral"


def has_cited_corroboration(analysis: ArticleAnalysis) -> bool:
    return any(
        item.status is EvidenceStatus.CORROBORATED and item.evidence_article_ids
        for item in analysis.claims
    )


def context_strength(analysis: ArticleAnalysis) -> float:
    """Evidence-context breadth with the embedded corroboration component removed."""

    embedded = EMBEDDED_CORROBORATION_BONUS if has_cited_corroboration(analysis) else 0.0
    return _clamp(analysis.evidence_strength - embedded)


def support_for(analysis: ArticleAnalysis) -> float:
    """How well established this analysis is, independent of how bad the event would be."""

    return _clamp(
        SUPPORT_CONFIDENCE_WEIGHT * analysis.event.model_confidence
        + SUPPORT_CONTEXT_WEIGHT * context_strength(analysis)
        + CLAIM_ADJUSTMENTS[claim_state(analysis)]
    )


def decay_for(analysis: ArticleAnalysis, now: datetime) -> float:
    """Exponential decay keyed on how long the economic consequences persist.

    ``time_horizon`` is persistence, not onset, so a durable exposure decays slowly. Age uses
    the publication timestamp, never ``analysis_created_at``, and a future timestamp clamps to
    age zero rather than amplifying the signal.
    """

    published_at = ensure_utc(analysis.source_reference.published_at)
    age_days = max(0.0, (ensure_utc(now) - published_at).total_seconds() / 86_400)
    half_life = HALF_LIFE_DAYS[analysis.event.time_horizon]
    return 0.5 ** (age_days / half_life)


def extract_risk_signals(
    analyses: Sequence[ArticleAnalysis],
    *,
    now: datetime,
) -> RiskSignalExtraction:
    """Return every downside signal supported by the stored analyses, with counters."""

    signals: list[RiskSignal] = []
    eligible = unmapped = realized = prospective = 0
    for analysis in analyses:
        if not is_meaningful_event(analysis.event):
            continue
        if reads_as_third_party_appointment(
            analysis.source_reference.title, analysis.subject_company
        ):
            # Not eligible rather than merely unscored: the downside described belongs to the
            # organisation doing the appointing, so counting it as an examined subject-company
            # analysis would overstate what the risk layer actually looked at.
            continue
        eligible += 1
        event = analysis.event
        if event.direction is EventDirection.UNCERTAIN:
            continue

        support = support_for(analysis)
        decay = decay_for(analysis, now)

        if event.direction is EventDirection.NEGATIVE:
            theme = theme_for_realized_negative_event(event.event_type, event.summary)
            if theme is not None:
                signals.append(
                    _build_signal(
                        analysis,
                        theme=theme,
                        basis=RiskSignalBasis.REALIZED_EVENT,
                        mechanism=event.summary,
                        realization=REALIZED_NEGATIVE_EVENT_FACTOR,
                        support=support,
                        decay=decay,
                    )
                )
                realized += 1

        realization = NEGATIVE_CHANNEL_FACTORS.get(event.direction)
        if realization is None:
            continue
        for mechanism in event.negative_channels:
            theme = theme_for_mechanism(mechanism)
            if theme is RiskTheme.UNMAPPED:
                unmapped += 1
                continue
            signals.append(
                _build_signal(
                    analysis,
                    theme=theme,
                    basis=RiskSignalBasis.NEGATIVE_CHANNEL,
                    mechanism=mechanism,
                    realization=realization,
                    support=support,
                    decay=decay,
                )
            )
            prospective += 1

    diagnostics = RiskDiagnostics(
        considered_analyses=len(analyses),
        eligible_analyses=eligible,
        signals_extracted=len(signals),
        realized_signals=realized,
        prospective_signals=prospective,
        unmapped_signals=unmapped,
    )
    return RiskSignalExtraction(tuple(signals), diagnostics)


def _build_signal(
    analysis: ArticleAnalysis,
    *,
    theme: RiskTheme,
    basis: RiskSignalBasis,
    mechanism: str,
    realization: float,
    support: float,
    decay: float,
) -> RiskSignal:
    # Extraction confidence stays out of severity: it belongs to how well established the
    # observation is, which is already the support axis.
    severity = _clamp(analysis.event.magnitude * realization)
    return RiskSignal(
        theme=theme,
        article_id=analysis.article_id,
        publisher=analysis.source_reference.publisher,
        title=analysis.source_reference.title,
        url=analysis.source_reference.url,
        published_at=ensure_utc(analysis.source_reference.published_at),
        source_class=analysis.source_class,
        basis=basis,
        mechanism=mechanism,
        magnitude=analysis.event.magnitude,
        severity=severity,
        support=support,
        decay=decay,
        score=_clamp(severity * support * decay),
    )
