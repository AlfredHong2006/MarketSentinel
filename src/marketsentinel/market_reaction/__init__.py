"""Pure deterministic `mr-v1` historical market reaction engine.

Methodology source of truth: docs/product/HISTORICAL_MARKET_REACTION_V1.md. This package owns no
API, snapshot, persistence, or presentation concern.
"""

from marketsentinel.market_reaction.calendars import (
    SessionCalendar,
    benchmark_symbol,
    build_session_calendar,
    reference_index_symbol,
    resolve_exchange,
)
from marketsentinel.market_reaction.engine import analyze_market_reaction, resolve_thresholds
from marketsentinel.market_reaction.models import (
    METHODOLOGY_VERSION,
    MR_V1_FROZEN_THRESHOLDS,
    DataQualityPolicy,
    EventStatus,
    EvidenceState,
    HistorySufficiency,
    ListingExchange,
    MarketReactionResult,
    PathCohort,
    PriceObservation,
    ReactionArticle,
    Regime,
    RegimeThresholds,
    ThresholdSelection,
    ThresholdStatus,
    TimestampQuality,
    TimingClass,
)
from marketsentinel.market_reaction.signal import (
    build_session_signals,
    from_scored_article,
    normalize_title,
    select_thresholds,
    selection_population,
)

__all__ = [
    "METHODOLOGY_VERSION",
    "MR_V1_FROZEN_THRESHOLDS",
    "DataQualityPolicy",
    "EventStatus",
    "EvidenceState",
    "HistorySufficiency",
    "ListingExchange",
    "MarketReactionResult",
    "PathCohort",
    "PriceObservation",
    "ReactionArticle",
    "Regime",
    "RegimeThresholds",
    "SessionCalendar",
    "ThresholdSelection",
    "ThresholdStatus",
    "TimestampQuality",
    "TimingClass",
    "analyze_market_reaction",
    "benchmark_symbol",
    "build_session_calendar",
    "build_session_signals",
    "from_scored_article",
    "normalize_title",
    "reference_index_symbol",
    "resolve_exchange",
    "resolve_thresholds",
    "select_thresholds",
    "selection_population",
]
