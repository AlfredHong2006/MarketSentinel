"""Deterministic, conservative synopsis for the Current Market View section.

Each observation below is computed independently from already-validated data produced
by dashboard_intelligence.py and dashboard_risks.py. This module never combines them into
an overall score, verdict, or bullish/bearish stance, and never asserts a causal link
between sections unless the underlying structured data already states one.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from marketsentinel.dashboard_intelligence import IntelligenceCard
from marketsentinel.dashboard_risks import EMPTY_TOP_RISKS_MESSAGE, RiskRow

NO_PRICE_MESSAGE = "Price is not currently available for this company."
NO_SENTIMENT_MESSAGE = "No recent news-sentiment observations are available."
NO_RISK_MESSAGE = EMPTY_TOP_RISKS_MESSAGE
NO_INTELLIGENCE_MESSAGE = "No high-confidence analysed events available yet."

_SPARSE_SENTIMENT_OBSERVATIONS = 5
_SENTIMENT_NEUTRAL_BAND = 0.05
_SENTIMENT_TREND_BAND = 0.05


@dataclass(frozen=True)
class MarketViewSummary:
    """Four independently-computed, non-composite observations. No overall verdict."""

    price_note: str
    sentiment_note: str
    risk_note: str
    intelligence_note: str


def price_note(points: Sequence[Mapping[str, Any]]) -> str:
    if not points:
        return NO_PRICE_MESSAGE
    latest = float(points[-1]["close"])
    if len(points) > 1 and float(points[-2]["close"]):
        delta = latest / float(points[-2]["close"]) - 1
        direction = "up" if delta >= 0 else "down"
        return f"Price is {latest:,.2f}, {direction} {abs(delta):.2%} from the previous session."
    return f"Price is {latest:,.2f}."


def sentiment_note(daily_sentiment: Sequence[Mapping[str, Any]]) -> str:
    if not daily_sentiment:
        return NO_SENTIMENT_MESSAGE
    latest = daily_sentiment[-1]
    average = float(latest["moving_average_7d"])
    trend = float(latest.get("trend_3", 0.0))
    if average > _SENTIMENT_NEUTRAL_BAND:
        level = "positive"
    elif average < -_SENTIMENT_NEUTRAL_BAND:
        level = "negative"
    else:
        level = "neutral"
    if trend > _SENTIMENT_TREND_BAND:
        movement = ", trending more positive across its most recent observations"
    elif trend < -_SENTIMENT_TREND_BAND:
        movement = ", trending more negative across its most recent observations"
    else:
        movement = ""
    coverage_qualifier = ""
    if len(daily_sentiment) < _SPARSE_SENTIMENT_OBSERVATIONS:
        coverage_qualifier = " (based on a small number of recent observations)"
    return (
        f"Recent observed news sentiment is {level} (7-day average {average:+.2f})"
        f"{movement}{coverage_qualifier}. This reflects only the available short-run window, "
        "not a multi-month trend."
    )


def risk_note(rows: Sequence[RiskRow]) -> str:
    if not rows:
        return NO_RISK_MESSAGE
    top = rows[0]
    return (
        f"The highest currently evidenced downside concern is {top.label}, "
        f"with a Concern Index of {top.concern_index} ({top.band})."
    )


def intelligence_note(cards: Sequence[IntelligenceCard]) -> str:
    if not cards:
        return NO_INTELLIGENCE_MESSAGE
    top = cards[0]
    direction = top.event.event.direction.value
    event_type = top.event.event.event_type.value.replace("_", " ")
    return (
        f"The latest material analysed event is a {direction} {event_type} "
        f"({top.impact_label}, evidence: {top.evidence_label})."
    )


def build_market_view(
    *,
    price_points: Sequence[Mapping[str, Any]],
    daily_sentiment: Sequence[Mapping[str, Any]],
    risk_rows: Sequence[RiskRow],
    intelligence_cards: Sequence[IntelligenceCard],
) -> MarketViewSummary:
    """Compose four independent, deterministic observations. No combined score is produced."""

    return MarketViewSummary(
        price_note=price_note(price_points),
        sentiment_note=sentiment_note(daily_sentiment),
        risk_note=risk_note(risk_rows),
        intelligence_note=intelligence_note(intelligence_cards),
    )
