"""Typed inputs, policy constants, and versioned results for the `mr-v1` engine.

The numeric constants here are the frozen design inputs of
docs/product/HISTORICAL_MARKET_REACTION_V1.md. They are methodology, not tuning knobs: changing
one changes what MarketSentinel claims and needs a new methodology version once `mr-v1` is frozen.
"""

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

METHODOLOGY_VERSION = "mr-v1"
TITLE_NORMALIZATION_VERSION = "mr-title-v1"

# Thresholds are asymmetric: one per regime, chosen from the two tails of the pooled session
# marginal separately, because real news sentiment is skewed and a single symmetric threshold
# silently puts very different shares of sessions in each tail.
THRESHOLD_FLOOR = 0.20
THRESHOLD_POSITIVE_QUANTILE = 0.85
THRESHOLD_NEGATIVE_QUANTILE = 0.15
# Published thresholds are rounded to two decimals so the selected value is a stable, quotable
# number rather than a long float that appears to carry more precision than the sample supports.
THRESHOLD_DECIMALS = 2

MIN_DISTINCT_SOURCES = 3
PRIMARY_HORIZON = 5
MAX_PATH_HORIZON = 10

BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_CONFIDENCE = 0.95

# History sufficiency is all three together: a long enough span, covered densely enough, with
# enough sessions that could actually qualify as events. Span alone admits two clusters of news
# separated by a year of silence.
MIN_HISTORY_SESSIONS = 126
MIN_SIGNAL_DENSITY = 0.50
MIN_QUALIFIED_SIGNAL_SESSIONS = 63

MIN_EVENTS_PRELIMINARY = 10
MIN_EVENTS_VERDICT = 20
MIN_ABS_MEAN_RETURN = 0.005

SPEARMAN_MIN_SESSIONS = 100

# Review flags only: a flagged observation stays in the sample (spec section 12). The stock level
# sits far above an ordinary single-session move and below the -50% / +100% signature of an
# unadjusted split; the benchmark level is the first US market-wide circuit-breaker step.
EXTREME_STOCK_SESSION_MOVE = 0.20
EXTREME_BENCHMARK_SESSION_MOVE = 0.07


class ListingExchange(StrEnum):
    US = "XNYS"
    LONDON = "XLON"


class TimestampQuality(StrEnum):
    FULL = "full"
    DATE_ONLY = "date_only"
    UNUSABLE = "unusable"


class TimingClass(StrEnum):
    """Whether a session's own signal was built from trustworthy publication times.

    `exact` means every kept article in the session carried a full timestamp. `lagged` means at
    least one was date-only and therefore conservatively rolled forward, so the session is very
    likely the one *after* the news rather than the one that reacted to it.
    """

    EXACT = "exact"
    LAGGED = "lagged"


class Regime(StrEnum):
    CLEARLY_POSITIVE = "clearly_positive"
    CLEARLY_NEGATIVE = "clearly_negative"


class PathCohort(StrEnum):
    """Which resolved events a path horizon was aggregated over.

    Day 0 and the forward horizons deliberately use different cohorts, so every horizon states
    its own cohort and its own `n` instead of leaving a consumer to assume they match.
    """

    EXACT_TIMING = "exact_timing"
    ALL_RESOLVED = "all_resolved"


class EventStatus(StrEnum):
    RESOLVED = "resolved"
    # The forward window runs past the last available price: it will resolve with time.
    PENDING = "pending"
    # A required stock or benchmark price inside the available range is absent. Never imputed.
    MISSING_PRICE_DATA = "missing_price_data"
    SUPPRESSED_OVERLAP = "suppressed_overlap"
    EXCLUDED_INVALID = "excluded_invalid"


class EvidenceState(StrEnum):
    DATA_QUALITY_INADEQUATE = "data_quality_inadequate"
    NOT_ENOUGH_HISTORY = "not_enough_history"
    INSUFFICIENT_EVENTS = "insufficient_events"
    PRELIMINARY = "preliminary"
    NO_CONSISTENT_RELATIONSHIP = "no_consistent_relationship"
    UNSTABLE = "unstable"
    DETECTED = "detected"


class ThresholdStatus(StrEnum):
    FROZEN = "frozen"
    PROVISIONAL = "provisional"


class RegimeThresholds(BaseModel):
    """The two regime entry thresholds, both stored as positive magnitudes.

    A session is clearly positive when `S_t >= positive` and clearly negative when
    `S_t <= -negative`.
    """

    model_config = ConfigDict(frozen=True)

    negative: float = Field(ge=THRESHOLD_FLOOR, le=1)
    positive: float = Field(ge=THRESHOLD_FLOOR, le=1)


# The numeric thresholds are frozen during MR-003 validation, from pooled session-sentiment
# marginals only and before any outcome inspection. Until then this stays None and every caller
# must pass provisional thresholds explicitly; the result records which of the two it was.
MR_V1_FROZEN_THRESHOLDS: RegimeThresholds | None = None


class ReactionArticle(BaseModel):
    """One sentiment-scored article as the engine sees it.

    `published_at` carries a trustworthy instant; `published_date` alone means the provider gave a
    date with no trustworthy time-of-day. Neither means the publication time is unusable.
    """

    model_config = ConfigDict(frozen=True)

    article_id: str
    ticker: str
    title: str
    url: str | None = None
    source: str
    published_at: datetime | None = None
    published_date: date | None = None
    p_positive: float | None = None
    p_negative: float | None = None
    p_neutral: float | None = None
    is_demo: bool = False


class PriceObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: date
    adjusted_close: float


class DataQualityPolicy(BaseModel):
    """Optional ratio ceilings for `data_quality_inadequate`.

    The spec leaves the exact ceilings to MR-001/MR-003, so none is assumed here: None disables
    that check. Structural failures (unresolved exchange, no usable price series) always apply.
    """

    model_config = ConfigDict(frozen=True)

    max_unusable_timestamp_share: float | None = Field(default=None, ge=0, le=1)
    max_date_only_timestamp_share: float | None = Field(default=None, ge=0, le=1)
    max_stock_price_gap_share: float | None = Field(default=None, ge=0, le=1)
    max_benchmark_price_gap_share: float | None = Field(default=None, ge=0, le=1)
    max_missing_price_event_share: float | None = Field(default=None, ge=0, le=1)


class SessionSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    session: date
    signal: float = Field(ge=-1, le=1)
    article_count: int = Field(ge=1)
    distinct_source_count: int = Field(ge=1)
    # Articles assigned to the session before title deduplication, as breadth context only.
    assigned_article_count: int = Field(ge=1)
    date_only_share: float = Field(ge=0, le=1)
    timing_class: TimingClass
    article_ids: tuple[str, ...]
    sources: tuple[str, ...]


class ThresholdSelection(BaseModel):
    """Outcome-blind threshold selection from pooled session-signal marginals."""

    model_config = ConfigDict(frozen=True)

    methodology_version: str = METHODOLOGY_VERSION
    # Sessions in the selection population E: pooled signal-defined sessions that meet the
    # distinct-source floor. Sessions that could never qualify as events never shape the tails.
    population_count: int
    pooled_session_count: int
    positive_quantile: float
    negative_quantile: float
    floor: float
    raw_positive: float | None
    raw_negative: float | None
    thresholds: RegimeThresholds
    positive_floor_applied: bool
    negative_floor_applied: bool
    positive_tail_share: float
    negative_tail_share: float


class ReturnStatistics(BaseModel):
    model_config = ConfigDict(frozen=True)

    n: int
    mean: float
    median: float
    ci_low: float
    ci_high: float
    share_positive: float


class PathPoint(BaseModel):
    """One horizon of the descriptive reaction path.

    Horizon 0 is the news session's own close-to-close move: contemporaneous context, never
    predictive evidence, and aggregated over exact-timing events only. Horizons 1..10 are forward
    returns anchored at the news-session close over every resolved event. `cohort` and
    `statistics.n` state that difference rather than leaving it to be inferred.
    """

    model_config = ConfigDict(frozen=True)

    horizon: int
    contemporaneous: bool
    cohort: PathCohort
    statistics: ReturnStatistics


class ReactionEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    session: date
    regime: Regime
    status: EventStatus
    signal: float
    article_count: int
    distinct_source_count: int
    date_only_share: float
    timing_class: TimingClass
    article_ids: tuple[str, ...]
    sources: tuple[str, ...]
    suppressed_by: date | None = None
    exclusion_reason: str | None = None
    # Indexed by horizon 0..10; None where a required price is absent. Populated as far as the
    # data allows even for unresolved events, so a reviewer can see what is missing.
    stock_returns: tuple[float | None, ...] = ()
    benchmark_returns: tuple[float | None, ...] = ()
    market_adjusted_returns: tuple[float | None, ...] = ()
    primary_market_adjusted_return: float | None = None
    integrity_flags: tuple[str, ...] = ()


class RegimeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    regime: Regime
    state: EvidenceState
    threshold: float
    qualifying_event_count: int
    resolved_event_count: int
    pending_event_count: int
    missing_price_event_count: int
    suppressed_event_count: int
    excluded_event_count: int
    # Resolved events whose own session had exact publication times. Only these contribute to
    # the day-0 aggregate, so it is reported separately from `resolved_event_count`.
    day_zero_event_count: int = 0
    exact_timing_event_count: int = 0
    primary: ReturnStatistics | None = None
    # Ordered by horizon. Horizon 0 is absent when no resolved event had exact timing.
    path: tuple[PathPoint, ...] = ()
    first_half_mean: float | None = None
    second_half_mean: float | None = None
    ci_excludes_zero: bool | None = None
    meets_minimum_effect: bool | None = None
    split_half_same_sign: bool | None = None
    mean_median_same_sign: bool | None = None
    events: tuple[ReactionEvent, ...] = ()

    def point_at(self, horizon: int) -> PathPoint | None:
        return next((point for point in self.path if point.horizon == horizon), None)


class SpearmanResult(BaseModel):
    """Descriptive only; never feeds a verdict (spec section 14)."""

    model_config = ConfigDict(frozen=True)

    n: int
    rho: float


class HistorySufficiency(BaseModel):
    """The three inclusive history requirements, each reported with its own observed value."""

    model_config = ConfigDict(frozen=True)

    span_sessions: int
    signal_session_count: int
    qualified_signal_session_count: int
    signal_density: float
    min_span_sessions: int = MIN_HISTORY_SESSIONS
    min_signal_density: float = MIN_SIGNAL_DENSITY
    min_qualified_signal_sessions: int = MIN_QUALIFIED_SIGNAL_SESSIONS
    meets_span: bool
    meets_density: bool
    meets_qualified_count: bool

    @property
    def sufficient(self) -> bool:
        return self.meets_span and self.meets_density and self.meets_qualified_count


class DataQualityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange_resolved: bool
    input_article_count: int
    eligible_article_count: int
    ineligible_wrong_ticker: int
    ineligible_demo: int
    ineligible_invalid_sentiment: int
    ineligible_unusable_timestamp: int
    ineligible_outside_calendar: int
    full_timestamp_count: int
    date_only_timestamp_count: int
    unusable_timestamp_share: float
    date_only_timestamp_share: float
    canonical_duplicates_removed: int
    title_duplicates_removed: int
    lagged_signal_session_count: int
    stock_price_count: int
    benchmark_price_count: int
    stock_price_gap_sessions: int
    benchmark_price_gap_sessions: int
    stock_price_gap_share: float
    benchmark_price_gap_share: float
    invalid_price_observations: int
    off_calendar_price_dates: int
    unresolved_event_windows: int
    missing_price_event_share: float
    integrity_review_candidates: int
    reasons: tuple[str, ...] = ()


class RoleFilterPlacement(StrEnum):
    """Where the company-role eligibility rule is applied. Both are implemented; neither is chosen.

    ``before_signals``: articles that fail the rule never reach session-signal construction, so
    the session signal, its source count, history sufficiency, and the pooled threshold-selection
    population ``E`` are all computed from principal articles only.

    ``at_qualification``: session signals and ``E`` are exactly the unfiltered ones; the rule is
    applied when a tail session would become an event, which then also needs at least
    ``MIN_DISTINCT_SOURCES`` distinct sources among its principal articles.
    """

    BEFORE_SIGNALS = "before_signals"
    AT_QUALIFICATION = "at_qualification"


class UnlabelledPolicy(StrEnum):
    """How an article with no role label is treated. There is no silent default.

    ``exclude``: treated as not principal (it does not count). ``include``: counted as principal
    (the rule is applied to labelled articles only). ``session_ineligible``: any session holding an
    unlabelled article cannot become an event. None of these treats an unlabelled article as
    ``mentioned``; the report always states how many there were.
    """

    EXCLUDE = "exclude"
    INCLUDE = "include"
    SESSION_INELIGIBLE = "session_ineligible"


class RoleFilterReport(BaseModel):
    """What the company-role rule did, so a consumer can see its effect. Counts are over the
    company's non-demo input articles."""

    model_config = ConfigDict(frozen=True)

    rule: str = "principal_only"
    placement: RoleFilterPlacement
    unlabelled_policy: UnlabelledPolicy
    articles_considered: int
    articles_principal: int
    articles_mentioned: int
    # An article with no label: a different fact from `articles_mentioned`.
    articles_unlabelled: int
    # Articles the rule removed under this placement and policy.
    articles_excluded: int
    # before_signals + session_ineligible: sessions dropped for holding an unlabelled article.
    # at_qualification: tail sessions that could not become events under the rule.
    sessions_removed: int = 0


class MarketReactionResult(BaseModel):
    """Versioned, self-describing result suitable for later snapshot/API projection."""

    model_config = ConfigDict(frozen=True)

    methodology_version: str = METHODOLOGY_VERSION
    title_normalization_version: str = TITLE_NORMALIZATION_VERSION
    ticker: str
    exchange: ListingExchange | None
    benchmark_symbol: str | None
    thresholds: RegimeThresholds
    threshold_status: ThresholdStatus
    min_distinct_sources: int = MIN_DISTINCT_SOURCES
    primary_horizon: int = PRIMARY_HORIZON
    max_path_horizon: int = MAX_PATH_HORIZON
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES
    state: EvidenceState | None
    signal_session_count: int
    first_signal_session: date | None
    last_signal_session: date | None
    history: HistorySufficiency
    positive: RegimeResult
    negative: RegimeResult
    session_signals: tuple[SessionSignal, ...]
    spearman: SpearmanResult | None = None
    data_quality: DataQualityReport
    # Present only when a company-role filter was supplied; None means "no filter was applied".
    role_filter: RoleFilterReport | None = None
