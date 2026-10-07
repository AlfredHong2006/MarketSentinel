"""Orchestration of one company's `mr-v1` historical market reaction.

Pure and deterministic: no clock, no network, no persistence, no LLM. Everything the result needs
is an argument, and the same arguments always produce the same result.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta

from marketsentinel.market_reaction.calendars import (
    SessionCalendar,
    benchmark_symbol,
    build_session_calendar,
    resolve_exchange,
)
from marketsentinel.market_reaction.models import (
    MAX_PATH_HORIZON,
    MIN_DISTINCT_SOURCES,
    MIN_HISTORY_SESSIONS,
    MIN_QUALIFIED_SIGNAL_SESSIONS,
    MIN_SIGNAL_DENSITY,
    MR_V1_FROZEN_THRESHOLDS,
    PRIMARY_HORIZON,
    DataQualityPolicy,
    DataQualityReport,
    EventStatus,
    EvidenceState,
    HistorySufficiency,
    ListingExchange,
    MarketReactionResult,
    PathCohort,
    PathPoint,
    PriceObservation,
    ReactionArticle,
    ReactionEvent,
    Regime,
    RegimeResult,
    RegimeThresholds,
    RoleFilterPlacement,
    RoleFilterReport,
    SessionSignal,
    ThresholdStatus,
    TimingClass,
)
from marketsentinel.market_reaction.returns import SessionPrices, align_prices, reaction_path
from marketsentinel.market_reaction.role_filter import (
    RoleFilter,
    build_role_filtered_signals,
    finalize_qualification_report,
    unfiltered_role_report,
)
from marketsentinel.market_reaction.signal import (
    SignalDiagnostics,
    build_session_signals,
    classify_regime,
)
from marketsentinel.market_reaction.statistics import (
    bootstrap_seed,
    ci_excludes_zero,
    meets_minimum_effect,
    regime_state,
    same_sign,
    spearman,
    split_half_means,
    summarize_returns,
)

_CALENDAR_LEAD_DAYS = 14
# Must reach ten sessions past the last article even across a holiday cluster.
_CALENDAR_TAIL_DAYS = 35


def resolve_thresholds(
    thresholds: RegimeThresholds | None,
) -> tuple[RegimeThresholds, ThresholdStatus]:
    """Use the frozen pair once it exists; until then provisional thresholds must be explicit.

    The floor is enforced by `RegimeThresholds` itself, so a pair that would loosen either regime
    below 0.20 cannot be constructed at all.
    """

    value = MR_V1_FROZEN_THRESHOLDS if thresholds is None else thresholds
    if value is None:
        raise ValueError(
            "mr-v1 thresholds are not frozen yet; pass provisional RegimeThresholds explicitly"
        )
    frozen = MR_V1_FROZEN_THRESHOLDS is not None and value == MR_V1_FROZEN_THRESHOLDS
    return value, ThresholdStatus.FROZEN if frozen else ThresholdStatus.PROVISIONAL


def analyze_market_reaction(
    *,
    ticker: str,
    listing_market: str | None,
    articles: Iterable[ReactionArticle],
    stock_prices: Iterable[PriceObservation],
    benchmark_prices: Iterable[PriceObservation],
    thresholds: RegimeThresholds | None = None,
    calendar: SessionCalendar | None = None,
    integrity_exclusions: Mapping[date, str] | None = None,
    quality_policy: DataQualityPolicy | None = None,
    role_filter: RoleFilter | None = None,
) -> MarketReactionResult:
    """Run the full `mr-v1` procedure for one company.

    `integrity_exclusions` maps an event session to the recorded evidence that the observation is
    invalid (a feed or corporate-action fault). It is the only way an event leaves the sample; a
    large return alone only raises a review flag.

    `role_filter` applies the deterministic company-role eligibility rule (see `role_filter.py`).
    With `None` (the default) nothing about the procedure or the result changes; with a filter, the
    result's `role_filter` field reports how many articles the rule excluded and how many had no
    label. The thresholds are always an argument: under the `before_signals` placement the caller
    selects them from signals built with `build_role_filtered_signals`, so the selection population
    `E` is role-filtered too.
    """

    threshold_values, threshold_status = resolve_thresholds(thresholds)
    article_list = list(articles)
    stock_list = list(stock_prices)
    benchmark_list = list(benchmark_prices)
    exchange = calendar.exchange if calendar is not None else resolve_exchange(listing_market)
    if exchange is None:
        return _unresolved_exchange_result(
            ticker,
            threshold_values,
            threshold_status,
            len(article_list),
            None
            if role_filter is None
            else unfiltered_role_report(article_list, ticker, role_filter),
        )
    if calendar is None:
        calendar = _calendar_for(exchange, article_list, stock_list, benchmark_list)

    role_report: RoleFilterReport | None = None
    blocked_sessions: frozenset[date] = frozenset()
    if role_filter is None:
        built = build_session_signals(article_list, ticker, calendar) if calendar else None
    else:
        filtered = build_role_filtered_signals(article_list, ticker, calendar, role_filter)
        built, role_report, blocked_sessions = (
            filtered.signals,
            filtered.report,
            filtered.blocked_sessions,
        )
        if role_filter.placement is RoleFilterPlacement.AT_QUALIFICATION:
            role_report = finalize_qualification_report(
                filtered,
                sum(
                    1
                    for signal in built.signals
                    if signal.session in blocked_sessions
                    and classify_regime(signal, threshold_values) is not None
                ),
            )
    signals = built.signals if built else ()
    diagnostics = built.diagnostics if built else SignalDiagnostics(len(article_list))
    stock = _aligned(stock_list, calendar)
    benchmark = _aligned(benchmark_list, calendar)

    events = _build_events(
        signals,
        calendar,
        stock,
        benchmark,
        threshold_values,
        dict(integrity_exclusions or {}),
        blocked_sessions,
    )
    quality = _quality_report(diagnostics, stock, benchmark, events, quality_policy)
    history = _history_sufficiency(signals, calendar)
    if quality.reasons:
        company_state: EvidenceState | None = EvidenceState.DATA_QUALITY_INADEQUATE
    elif not history.sufficient:
        company_state = EvidenceState.NOT_ENOUGH_HISTORY
    else:
        company_state = None

    return MarketReactionResult(
        ticker=ticker,
        exchange=exchange,
        benchmark_symbol=benchmark_symbol(exchange),
        thresholds=threshold_values,
        threshold_status=threshold_status,
        state=company_state,
        signal_session_count=len(signals),
        first_signal_session=signals[0].session if signals else None,
        last_signal_session=signals[-1].session if signals else None,
        history=history,
        positive=_regime_result(
            Regime.CLEARLY_POSITIVE, threshold_values.positive, events, company_state
        ),
        negative=_regime_result(
            Regime.CLEARLY_NEGATIVE, threshold_values.negative, events, company_state
        ),
        session_signals=signals,
        spearman=_session_spearman(signals, calendar, stock, benchmark),
        data_quality=quality,
        role_filter=role_report,
    )


def _calendar_for(
    exchange: ListingExchange,
    articles: Sequence[ReactionArticle],
    stock: Sequence[PriceObservation],
    benchmark: Sequence[PriceObservation],
) -> SessionCalendar | None:
    dates = [item.date for item in (*stock, *benchmark)]
    for article in articles:
        if article.published_at is not None:
            dates.append(article.published_at.date())
        elif article.published_date is not None:
            dates.append(article.published_date)
    if not dates:
        return None
    return build_session_calendar(
        exchange,
        min(dates) - timedelta(days=_CALENDAR_LEAD_DAYS),
        max(dates) + timedelta(days=_CALENDAR_TAIL_DAYS),
    )


def _aligned(prices: Sequence[PriceObservation], calendar: SessionCalendar | None) -> SessionPrices:
    if calendar is None:
        return SessionPrices(closes={}, invalid_observations=0, off_calendar_dates=0)
    return align_prices(prices, calendar)


def _build_events(
    signals: Sequence[SessionSignal],
    calendar: SessionCalendar | None,
    stock: SessionPrices,
    benchmark: SessionPrices,
    thresholds: RegimeThresholds,
    exclusions: dict[date, str],
    role_blocked: frozenset[date] = frozenset(),
) -> list[ReactionEvent]:
    """Qualify, exclude, apply same-regime exclusivity, then resolve — in that order.

    Exclusivity is decided from session positions alone, before any return is looked at, so which
    events enter the sample can never depend on their outcomes. Two same-regime events overlap
    when their +5 return intervals (t, t+5] share a session, i.e. when they are fewer than five
    sessions apart. A recorded-invalid event holds no window.

    A session in `role_blocked` (the `at_qualification` company-role rule) never qualifies, so it
    is not an event at all and holds no exclusivity window either.
    """

    events: list[ReactionEvent] = []
    last_kept: dict[Regime, tuple[int, date]] = {}
    for signal in signals:
        regime = classify_regime(signal, thresholds)
        if regime is None or calendar is None or signal.session in role_blocked:
            continue
        position = calendar.index_of(signal.session)
        path = reaction_path(stock, benchmark, position)
        fields = {
            "session": signal.session,
            "regime": regime,
            "signal": signal.signal,
            "article_count": signal.article_count,
            "distinct_source_count": signal.distinct_source_count,
            "date_only_share": signal.date_only_share,
            "timing_class": signal.timing_class,
            "article_ids": signal.article_ids,
            "sources": signal.sources,
            "stock_returns": path.stock,
            "benchmark_returns": path.benchmark,
            "market_adjusted_returns": path.market_adjusted,
            "integrity_flags": path.integrity_flags,
        }
        if signal.session in exclusions:
            events.append(
                ReactionEvent(
                    **fields,
                    status=EventStatus.EXCLUDED_INVALID,
                    exclusion_reason=exclusions[signal.session],
                )
            )
            continue
        kept = last_kept.get(regime)
        if kept is not None and position - kept[0] < PRIMARY_HORIZON:
            events.append(
                ReactionEvent(
                    **fields, status=EventStatus.SUPPRESSED_OVERLAP, suppressed_by=kept[1]
                )
            )
            continue
        last_kept[regime] = (position, signal.session)
        if path.complete:
            events.append(
                ReactionEvent(
                    **fields,
                    status=EventStatus.RESOLVED,
                    primary_market_adjusted_return=path.market_adjusted[PRIMARY_HORIZON],
                )
            )
            continue
        ends = [prices.last_index for prices in (stock, benchmark)]
        still_open = all(end is not None for end in ends) and position + MAX_PATH_HORIZON > min(
            ends
        )
        events.append(
            ReactionEvent(
                **fields,
                status=EventStatus.PENDING if still_open else EventStatus.MISSING_PRICE_DATA,
            )
        )
    return events


def _regime_result(
    regime: Regime,
    threshold: float,
    events: Sequence[ReactionEvent],
    company_state: EvidenceState | None,
) -> RegimeResult:
    """Aggregate one regime.

    Day 0 and the forward horizons use different cohorts on purpose. A lagged event's session is
    very likely the one after the news, so its day-0 move is not the reaction to that news and it
    is excluded from the contemporaneous aggregate. Its forward horizons are still anchored at a
    close that is genuinely after publication, so it stays in +1..+10 and in the primary statistic.
    """

    own = tuple(event for event in events if event.regime is regime)
    resolved = [event for event in own if event.status is EventStatus.RESOLVED]
    exact = [event for event in resolved if event.timing_class is TimingClass.EXACT]
    counts = {status: sum(event.status is status for event in own) for status in EventStatus}
    primary = first_half = second_half = None
    path: tuple[PathPoint, ...] = ()
    if resolved:
        primary = summarize_returns(
            [event.primary_market_adjusted_return for event in resolved],
            bootstrap_seed(regime.value, f"h{PRIMARY_HORIZON}"),
        )
        points = []
        if exact:
            points.append(
                PathPoint(
                    horizon=0,
                    contemporaneous=True,
                    cohort=PathCohort.EXACT_TIMING,
                    statistics=summarize_returns(
                        [event.market_adjusted_returns[0] for event in exact],
                        bootstrap_seed(regime.value, "h0", PathCohort.EXACT_TIMING.value),
                    ),
                )
            )
        points.extend(
            PathPoint(
                horizon=horizon,
                contemporaneous=False,
                cohort=PathCohort.ALL_RESOLVED,
                statistics=primary
                if horizon == PRIMARY_HORIZON
                else summarize_returns(
                    [event.market_adjusted_returns[horizon] for event in resolved],
                    bootstrap_seed(regime.value, f"h{horizon}"),
                ),
            )
            for horizon in range(1, MAX_PATH_HORIZON + 1)
        )
        path = tuple(points)
    if len(resolved) >= 2:
        first_half, second_half = split_half_means(
            [event.primary_market_adjusted_return for event in resolved]
        )
    state = company_state or regime_state(primary, first_half, second_half)
    return RegimeResult(
        regime=regime,
        state=state,
        threshold=threshold,
        qualifying_event_count=len(own),
        resolved_event_count=len(resolved),
        day_zero_event_count=len(exact),
        exact_timing_event_count=sum(event.timing_class is TimingClass.EXACT for event in own),
        pending_event_count=counts[EventStatus.PENDING],
        missing_price_event_count=counts[EventStatus.MISSING_PRICE_DATA],
        suppressed_event_count=counts[EventStatus.SUPPRESSED_OVERLAP],
        excluded_event_count=counts[EventStatus.EXCLUDED_INVALID],
        primary=primary,
        path=path,
        first_half_mean=first_half,
        second_half_mean=second_half,
        ci_excludes_zero=ci_excludes_zero(primary) if primary else None,
        meets_minimum_effect=meets_minimum_effect(primary) if primary else None,
        split_half_same_sign=same_sign(first_half, second_half) if first_half is not None else None,
        mean_median_same_sign=same_sign(primary.mean, primary.median) if primary else None,
        events=own,
    )


def _history_sufficiency(
    signals: Sequence[SessionSignal], calendar: SessionCalendar | None
) -> HistorySufficiency:
    """Span, density, and qualified-session count, all three required.

    Density is measured over the covered window itself (span + 1 sessions), so two dense clusters
    separated by a long silence fail even though their span is wide.
    """

    if not signals or calendar is None:
        span = 0
    else:
        span = calendar.index_of(signals[-1].session) - calendar.index_of(signals[0].session)
    qualified = sum(signal.distinct_source_count >= MIN_DISTINCT_SOURCES for signal in signals)
    density = len(signals) / (span + 1) if signals else 0.0
    return HistorySufficiency(
        span_sessions=span,
        signal_session_count=len(signals),
        qualified_signal_session_count=qualified,
        signal_density=density,
        meets_span=span >= MIN_HISTORY_SESSIONS,
        meets_density=density >= MIN_SIGNAL_DENSITY,
        meets_qualified_count=qualified >= MIN_QUALIFIED_SIGNAL_SESSIONS,
    )


def _session_spearman(
    signals: Sequence[SessionSignal],
    calendar: SessionCalendar | None,
    stock: SessionPrices,
    benchmark: SessionPrices,
):
    if calendar is None:
        return None
    pairs = []
    for signal in signals:
        path = reaction_path(stock, benchmark, calendar.index_of(signal.session), PRIMARY_HORIZON)
        value = path.market_adjusted[PRIMARY_HORIZON]
        if value is not None:
            pairs.append((signal.signal, value))
    return spearman([pair[0] for pair in pairs], [pair[1] for pair in pairs])


def _quality_report(
    diagnostics: SignalDiagnostics,
    stock: SessionPrices,
    benchmark: SessionPrices,
    events: Sequence[ReactionEvent],
    policy: DataQualityPolicy | None,
) -> DataQualityReport:
    policy = policy or DataQualityPolicy()
    timed = diagnostics.full_timestamp + diagnostics.date_only_timestamp
    with_time_field = timed + diagnostics.unusable_timestamp
    unusable_share = diagnostics.unusable_timestamp / with_time_field if with_time_field else 0.0
    date_only_share = diagnostics.date_only_timestamp / timed if timed else 0.0
    windows = [
        event
        for event in events
        if event.status
        in (EventStatus.RESOLVED, EventStatus.PENDING, EventStatus.MISSING_PRICE_DATA)
    ]
    missing = sum(event.status is EventStatus.MISSING_PRICE_DATA for event in windows)
    missing_share = missing / len(windows) if windows else 0.0

    reasons: list[str] = []
    if not stock.closes:
        reasons.append("no_usable_stock_prices")
    if not benchmark.closes:
        reasons.append("no_usable_benchmark_prices")
    for label, observed, ceiling in (
        ("unusable_timestamp_share", unusable_share, policy.max_unusable_timestamp_share),
        ("date_only_timestamp_share", date_only_share, policy.max_date_only_timestamp_share),
        ("stock_price_gap_share", stock.gap_share(), policy.max_stock_price_gap_share),
        ("benchmark_price_gap_share", benchmark.gap_share(), policy.max_benchmark_price_gap_share),
        ("missing_price_event_share", missing_share, policy.max_missing_price_event_share),
    ):
        if ceiling is not None and observed > ceiling:
            reasons.append(f"{label}_above_ceiling")

    return DataQualityReport(
        exchange_resolved=True,
        input_article_count=diagnostics.input_article_count,
        eligible_article_count=diagnostics.eligible,
        ineligible_wrong_ticker=diagnostics.wrong_ticker,
        ineligible_demo=diagnostics.demo,
        ineligible_invalid_sentiment=diagnostics.invalid_sentiment,
        ineligible_unusable_timestamp=diagnostics.unusable_timestamp,
        ineligible_outside_calendar=diagnostics.outside_calendar,
        full_timestamp_count=diagnostics.full_timestamp,
        date_only_timestamp_count=diagnostics.date_only_timestamp,
        unusable_timestamp_share=unusable_share,
        date_only_timestamp_share=date_only_share,
        canonical_duplicates_removed=diagnostics.canonical_duplicates,
        title_duplicates_removed=diagnostics.title_duplicates,
        lagged_signal_session_count=diagnostics.lagged_sessions,
        stock_price_count=len(stock.closes),
        benchmark_price_count=len(benchmark.closes),
        stock_price_gap_sessions=stock.gap_sessions(),
        benchmark_price_gap_sessions=benchmark.gap_sessions(),
        stock_price_gap_share=stock.gap_share(),
        benchmark_price_gap_share=benchmark.gap_share(),
        invalid_price_observations=stock.invalid_observations + benchmark.invalid_observations,
        off_calendar_price_dates=stock.off_calendar_dates + benchmark.off_calendar_dates,
        unresolved_event_windows=sum(
            event.status in (EventStatus.PENDING, EventStatus.MISSING_PRICE_DATA)
            for event in windows
        ),
        missing_price_event_share=missing_share,
        integrity_review_candidates=sum(bool(event.integrity_flags) for event in events),
        reasons=tuple(reasons),
    )


def _unresolved_exchange_result(
    ticker: str,
    thresholds: RegimeThresholds,
    threshold_status: ThresholdStatus,
    article_count: int,
    role_report: RoleFilterReport | None = None,
) -> MarketReactionResult:
    state = EvidenceState.DATA_QUALITY_INADEQUATE
    empty = {
        "state": state,
        "qualifying_event_count": 0,
        "resolved_event_count": 0,
        "pending_event_count": 0,
        "missing_price_event_count": 0,
        "suppressed_event_count": 0,
        "excluded_event_count": 0,
    }
    zeros = dict.fromkeys(
        (
            name
            for name, info in DataQualityReport.model_fields.items()
            if info.annotation in (int, float)
        ),
        0,
    )
    zeros["input_article_count"] = article_count
    return MarketReactionResult(
        ticker=ticker,
        exchange=None,
        benchmark_symbol=None,
        thresholds=thresholds,
        threshold_status=threshold_status,
        state=state,
        signal_session_count=0,
        first_signal_session=None,
        last_signal_session=None,
        history=_history_sufficiency((), None),
        positive=RegimeResult(
            regime=Regime.CLEARLY_POSITIVE, threshold=thresholds.positive, **empty
        ),
        negative=RegimeResult(
            regime=Regime.CLEARLY_NEGATIVE, threshold=thresholds.negative, **empty
        ),
        session_signals=(),
        data_quality=DataQualityReport(
            exchange_resolved=False, reasons=("unresolved_exchange",), **zeros
        ),
        role_filter=role_report,
    )
