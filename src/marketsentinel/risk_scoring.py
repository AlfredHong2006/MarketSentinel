"""Aggregation of risk signals into a bounded, explainable Concern Index per theme.

Two properties matter most here. Aggregation takes the strongest signal rather than a sum, so
repeated reporting of one event cannot inflate a theme. Corroboration is a small bounded bonus
on top of that maximum, so even a total failure to group duplicates can move a score by at most
0.16. The result is an evidence-weighted salience index, never a probability of loss.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from marketsentinel.domain import (
    ArticleAnalysis,
    RankedRisk,
    RiskDiagnostics,
    RiskTheme,
    SourceClass,
)
from marketsentinel.normalization import normalize_text
from marketsentinel.risk_signals import RiskSignal, extract_risk_signals

MAX_TOP_RISKS = 4
CORROBORATION_SCORE_FLOOR = 0.50
PUBLISHER_BONUS_STEP = 0.04
EVENT_BONUS_STEP = 0.04
MAX_BONUS_STEPS = 2
SEVERE_BAND_THRESHOLD = 69
GROUPING_WINDOW = timedelta(hours=48)
GROUPING_OVERLAP = 0.75
_DISTINCTIVE_TERM_LENGTH = 4
_HIGH_TRUST_SOURCES = frozenset({SourceClass.OFFICIAL_COMPANY, SourceClass.REGULATORY_OR_FILING})

_TITLE_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "amid",
        "and",
        "for",
        "from",
        "into",
        "says",
        "that",
        "the",
        "this",
        "with",
    }
)


@dataclass(frozen=True)
class RiskRanking:
    top_risks: tuple[RankedRisk, ...]
    diagnostics: RiskDiagnostics


def band_for(concern_index: int) -> str:
    if concern_index >= 70:
        return "Severe"
    if concern_index >= 50:
        return "Elevated"
    if concern_index >= 30:
        return "Moderate"
    return "Watch"


def title_terms(title: str) -> frozenset[str]:
    """Substantive stemmed title tokens used only for conservative duplicate grouping."""

    return frozenset(
        _stem(term)
        for term in re.findall(r"[a-z0-9]{3,}", title.casefold())
        if term not in _TITLE_STOPWORDS
    )


def _stem(term: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if term.endswith(suffix) and len(term) - len(suffix) >= 4:
            return term[: -len(suffix)]
    return term


def describes_same_event(first: RiskSignal, second: RiskSignal) -> bool:
    """Whether two signals plausibly describe one underlying event.

    Grouping is deliberately conservative and prefers to leave events separate: the
    corroboration bonus is bounded, so under-grouping costs at most a few points while
    over-grouping would silently erase a genuinely distinct event. Two headlines that each
    carry their own distinctive term are therefore treated as different events even when the
    rest of the wording matches, which keeps "deal with Toyota" apart from "deal with Honda"
    and "Arizona plant" apart from "Texas plant".
    """

    if first.theme is not second.theme:
        return False
    if abs(first.published_at - second.published_at) > GROUPING_WINDOW:
        return False
    left, right = title_terms(first.title), title_terms(second.title)
    if not left or not right:
        return False
    overlap = len(left & right) / min(len(left), len(right))
    if overlap < GROUPING_OVERLAP:
        return False
    only_left = {term for term in left - right if len(term) >= _DISTINCTIVE_TERM_LENGTH}
    only_right = {term for term in right - left if len(term) >= _DISTINCTIVE_TERM_LENGTH}
    return not (only_left and only_right)


def _event_groups(signals: Sequence[RiskSignal]) -> list[list[RiskSignal]]:
    """Group signals by transitive same-event similarity, in deterministic input order."""

    groups: list[list[RiskSignal]] = []
    for signal in signals:
        for group in groups:
            if any(describes_same_event(signal, member) for member in group):
                group.append(signal)
                break
        else:
            groups.append([signal])
    return groups


def rank_company_risks(
    analyses: Sequence[ArticleAnalysis],
    *,
    now: datetime,
    limit: int = MAX_TOP_RISKS,
) -> RiskRanking:
    """Rank downside themes for one company from its compatible stored analyses."""

    extraction = extract_risk_signals(analyses, now=now)
    by_theme: dict[RiskTheme, list[RiskSignal]] = {}
    for signal in extraction.signals:
        if signal.theme is RiskTheme.UNMAPPED:
            continue
        by_theme.setdefault(signal.theme, []).append(signal)

    ranked: list[RankedRisk] = []
    total_groups = capped = 0
    for theme, signals in by_theme.items():
        ordered = sorted(signals, key=_signal_sort_key)
        base = max(signal.score for signal in ordered)
        # Only signals close to the strongest one may corroborate it. A weak, unrelated
        # mention must not earn a theme extra credit.
        qualifying = [
            signal for signal in ordered if signal.score >= CORROBORATION_SCORE_FLOOR * base
        ]
        groups = _event_groups(qualifying)
        total_groups += len(groups)
        publishers = {normalize_text(signal.publisher) for signal in qualifying}

        publisher_bonus = PUBLISHER_BONUS_STEP * min(MAX_BONUS_STEPS, max(0, len(publishers) - 1))
        event_bonus = EVENT_BONUS_STEP * min(MAX_BONUS_STEPS, max(0, len(groups) - 1))
        theme_score = min(1.0, base + publisher_bonus + event_bonus)

        concern_index = round(theme_score * 100)
        if concern_index > SEVERE_BAND_THRESHOLD and not _passes_severe_band_gate(
            qualifying, publishers
        ):
            concern_index = SEVERE_BAND_THRESHOLD
            capped += 1
        if concern_index < 1:
            continue

        # Provenance describes the evidence that actually earned the score, so it is built from
        # the qualifying set rather than every signal in the theme. The strongest signal is always
        # qualifying, so the primary article is never hidden.
        strongest = qualifying[0]
        ranked.append(
            RankedRisk(
                theme=theme,
                concern_index=concern_index,
                band=band_for(concern_index),
                summary=strongest.mechanism,
                primary_article_id=strongest.article_id,
                primary_article_url=strongest.url,
                primary_publisher=strongest.publisher,
                first_evidenced_at=min(signal.published_at for signal in qualifying),
                latest_published_at=max(signal.published_at for signal in qualifying),
                supporting_article_ids=_unique_preserving_order(
                    signal.article_id for signal in qualifying
                ),
                supporting_publishers=_unique_preserving_order(
                    signal.publisher for signal in qualifying
                ),
                supporting_signal_count=len(qualifying),
                supporting_event_group_count=len(groups),
            )
        )

    ranked.sort(key=_risk_sort_key)
    selected = ranked[:limit]
    diagnostics = extraction.diagnostics.model_copy(
        update={
            # Themes produced by ranking, before the display cap. The length of ``top_risks``
            # already reports how many are shown.
            "themes_ranked": len(ranked),
            "event_groups": total_groups,
            "severe_band_capped": capped,
        }
    )
    return RiskRanking(tuple(selected), diagnostics)


def _passes_severe_band_gate(qualifying: Sequence[RiskSignal], publishers: set[str]) -> bool:
    """A single general-news article may not on its own produce a Severe risk."""

    if len(publishers) >= 2:
        return True
    return any(signal.source_class in _HIGH_TRUST_SOURCES for signal in qualifying)


def _signal_sort_key(signal: RiskSignal) -> tuple[float, float, str]:
    return (-signal.score, -signal.published_at.timestamp(), signal.article_id)


def _risk_sort_key(risk: RankedRisk) -> tuple[int, float, str]:
    return (-risk.concern_index, -risk.latest_published_at.timestamp(), risk.theme.value)


def _unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
