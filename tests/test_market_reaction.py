"""Offline, deterministic tests for the `mr-v1` historical market reaction engine."""

import inspect
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from marketsentinel.domain import ScoredArticle
from marketsentinel.market_reaction import (
    METHODOLOGY_VERSION,
    DataQualityPolicy,
    EventStatus,
    EvidenceState,
    ListingExchange,
    PathCohort,
    PriceObservation,
    ReactionArticle,
    Regime,
    RegimeThresholds,
    ThresholdStatus,
    TimestampQuality,
    TimingClass,
    analyze_market_reaction,
    benchmark_symbol,
    build_session_calendar,
    build_session_signals,
    from_scored_article,
    normalize_title,
    reference_index_symbol,
    resolve_exchange,
    resolve_thresholds,
    select_thresholds,
    selection_population,
)
from marketsentinel.market_reaction.models import IntervalMethod
from marketsentinel.market_reaction.returns import align_prices, reaction_path
from marketsentinel.market_reaction.signal import (
    article_polarity,
    classify_regime,
    has_valid_sentiment,
    split_publication_stamp,
    timestamp_quality,
)
from marketsentinel.market_reaction.statistics import (
    bootstrap_mean_ci,
    bootstrap_seed,
    regime_state,
    spearman,
    split_half_means,
    summarize_returns,
)

THRESHOLDS = RegimeThresholds(negative=0.30, positive=0.30)
US = build_session_calendar(ListingExchange.US, date(2021, 12, 1), date(2025, 1, 31))
UK = build_session_calendar(ListingExchange.LONDON, date(2021, 12, 1), date(2025, 1, 31))
# First US session of 2022; synthetic scenarios are laid out by session offset from here.
ORIGIN = US.index_of(date(2022, 1, 3))


def article(
    article_id: str,
    published_at: datetime | None,
    *,
    positive: float = 0.8,
    negative: float = 0.1,
    source: str | None = None,
    title: str | None = None,
    ticker: str = "ACME",
    url: str | None = None,
    published_date: date | None = None,
    is_demo: bool = False,
) -> ReactionArticle:
    return ReactionArticle(
        article_id=article_id,
        ticker=ticker,
        title=title or f"Acme headline {article_id}",
        url=url or f"https://example.com/{article_id}",
        source=source or f"Source {article_id}",
        published_at=published_at,
        published_date=published_date,
        p_positive=positive,
        p_negative=negative,
        p_neutral=round(1 - positive - negative, 6),
        is_demo=is_demo,
    )


def session_articles(
    position: int,
    *,
    positive: float = 0.8,
    negative: float = 0.1,
    sources: int = 3,
    date_only: bool = False,
) -> list[ReactionArticle]:
    """`sources` distinct-source articles published two hours before that US session's close.

    With `date_only` the same session is reached by the conservative rule instead, which makes it
    a lagged session: the articles are stamped with the previous calendar day and no time.
    """

    moment = US.closes[position] - timedelta(hours=2)
    stamp = (
        {"published_at": None, "published_date": US.sessions[position - 1]}
        if date_only
        else {"published_at": moment}
    )
    return [
        article(
            f"{'d' if date_only else 's'}{position}-{number}",
            positive=positive,
            negative=negative,
            **stamp,
        )
        for number in range(sources)
    ]


def filler_sessions(start: int, stop: int, skip: set[int], sources: int = 3):
    """Neutral signal-defined sessions that satisfy the history-density rule without qualifying.

    Their polarity is exactly zero, so they can never enter a regime at any admissible threshold.
    """

    return [
        item
        for position in range(start, stop)
        if position not in skip
        for item in session_articles(position, positive=0.45, negative=0.45, sources=sources)
    ]


def flat_prices(start: int, stop: int, value: float = 100.0) -> dict[int, float]:
    return {position: value for position in range(start, stop)}


def observations(closes: dict[int, float]) -> list[PriceObservation]:
    return [
        PriceObservation(date=US.sessions[position], adjusted_close=value)
        for position, value in sorted(closes.items())
    ]


def scenario(
    returns: list[float],
    *,
    positive: bool = True,
    spacing: int = 12,
    dense: bool = True,
    date_only: bool = False,
):
    """One qualifying event per entry whose +5 market-adjusted return is exactly that entry.

    The benchmark is flat and the stock sits at 100 except for sessions t+1..t+10 of each event.
    `dense` fills every non-event session with neutral news so the company clears the history
    requirements and the test is about the regime rule rather than about history.
    """

    closes = flat_prices(ORIGIN - 1, ORIGIN + spacing * len(returns) + 12)
    event_positions = {ORIGIN + number * spacing for number in range(len(returns))}
    articles: list[ReactionArticle] = []
    for number, value in enumerate(returns):
        position = ORIGIN + number * spacing
        for offset in range(1, 11):
            closes[position + offset] = 100.0 * (1 + value)
        articles += session_articles(
            position,
            positive=0.8 if positive else 0.1,
            negative=0.1 if positive else 0.8,
            date_only=date_only,
        )
    if dense:
        articles += filler_sessions(ORIGIN, max(event_positions) + 1, event_positions)
    return articles, observations(closes), observations(flat_prices(ORIGIN - 1, max(closes) + 1))


def run(articles, stock, benchmark, **kwargs):
    kwargs.setdefault("thresholds", THRESHOLDS)
    return analyze_market_reaction(
        ticker="ACME",
        listing_market="S&P 500",
        articles=articles,
        stock_prices=stock,
        benchmark_prices=benchmark,
        calendar=US,
        **kwargs,
    )


# --- information-time alignment -------------------------------------------------------------


def assigned(calendar, *moment) -> date:
    return calendar.sessions[calendar.assign_instant(datetime(*moment, tzinfo=UTC))]


def test_article_during_session_is_assigned_to_that_session():
    assert assigned(US, 2024, 3, 4, 15, 0) == date(2024, 3, 4)


def test_article_after_close_rolls_to_next_session():
    assert assigned(US, 2024, 3, 4, 21, 30) == date(2024, 3, 5)


def test_article_exactly_at_close_rolls_to_next_session():
    assert assigned(US, 2024, 3, 4, 21, 0) == date(2024, 3, 5)


def test_pre_market_article_belongs_to_the_same_day_session():
    assert assigned(US, 2024, 3, 4, 9, 0) == date(2024, 3, 4)


def test_weekend_article_is_assigned_to_monday():
    assert assigned(US, 2024, 3, 2, 12, 0) == date(2024, 3, 4)


def test_us_holiday_and_early_close():
    # Independence Day 2024 is closed; 3 July closes early at 13:00 New York (17:00 UTC).
    assert assigned(US, 2024, 7, 4, 15, 0) == date(2024, 7, 5)
    assert assigned(US, 2024, 7, 3, 16, 30) == date(2024, 7, 3)
    assert assigned(US, 2024, 7, 3, 17, 30) == date(2024, 7, 5)


def test_london_holiday_and_early_close():
    # Christmas Eve closes at 12:30 London; 25 and 26 December are holidays.
    assert assigned(UK, 2024, 12, 24, 12, 0) == date(2024, 12, 24)
    assert assigned(UK, 2024, 12, 24, 13, 0) == date(2024, 12, 27)
    # Summer bank holiday Monday.
    assert assigned(UK, 2024, 8, 26, 9, 0) == date(2024, 8, 27)


def test_us_uk_daylight_saving_divergence():
    # 11 March 2024: the US is already on DST (close 20:00 UTC) while the UK is not (16:30 UTC).
    assert US.closes[US.index_of(date(2024, 3, 11))] == datetime(2024, 3, 11, 20, 0, tzinfo=UTC)
    assert UK.closes[UK.index_of(date(2024, 3, 11))] == datetime(2024, 3, 11, 16, 30, tzinfo=UTC)
    assert assigned(US, 2024, 3, 11, 20, 30) == date(2024, 3, 12)
    # One week earlier the same UTC instant was still inside the US session.
    assert assigned(US, 2024, 3, 4, 20, 30) == date(2024, 3, 4)
    # After the UK also moves (31 March), London closes at 15:30 UTC.
    assert assigned(UK, 2024, 4, 2, 16, 0) == date(2024, 4, 3)
    assert assigned(UK, 2024, 3, 11, 16, 0) == date(2024, 3, 11)


def test_date_only_publication_cannot_leak_into_its_own_session():
    # Conservative rule: end of the exchange-local day, so the next session at the earliest.
    assert US.sessions[US.assign_date_only(date(2024, 3, 4))] == date(2024, 3, 5)
    assert US.sessions[US.assign_date_only(date(2024, 3, 1))] == date(2024, 3, 4)
    assert US.sessions[US.assign_date_only(date(2024, 7, 3))] == date(2024, 7, 5)


def test_instants_outside_the_calendar_are_not_assigned():
    assert US.assign_instant(datetime(2021, 11, 1, tzinfo=UTC)) is None
    assert US.assign_instant(datetime(2025, 6, 1, tzinfo=UTC)) is None
    with pytest.raises(ValueError):
        US.assign_instant(datetime(2024, 3, 4, 15, 0))


def test_exchange_and_benchmark_resolution():
    assert resolve_exchange("S&P 500") is ListingExchange.US
    assert resolve_exchange("FTSE 100") is ListingExchange.LONDON
    assert resolve_exchange("Nikkei 225") is None
    assert resolve_exchange(None) is None
    # Investable total-return trackers, so the benchmark leg is adjusted like the stock leg.
    assert benchmark_symbol(ListingExchange.US) == "SPY"
    assert benchmark_symbol(ListingExchange.LONDON) == "CUKX.L"
    # The headline price indices stay available for validation, never as the engine's input.
    assert reference_index_symbol(ListingExchange.US) == "^GSPC"
    assert reference_index_symbol(ListingExchange.LONDON) == "^FTSE"
    assert benchmark_symbol(ListingExchange.US) != reference_index_symbol(ListingExchange.US)


# --- polarity, eligibility, deduplication, session signal ------------------------------------


def test_article_polarity_is_positive_minus_negative():
    assert article_polarity(0.7, 0.2) == pytest.approx(0.5)
    assert article_polarity(0.0, 1.0) == -1.0


def test_sentiment_validity():
    moment = datetime(2024, 3, 4, 15, tzinfo=UTC)
    assert has_valid_sentiment(article("a", moment))
    assert not has_valid_sentiment(article("a", moment).model_copy(update={"p_positive": None}))
    assert not has_valid_sentiment(
        article("a", moment).model_copy(update={"p_positive": float("nan")})
    )
    assert not has_valid_sentiment(article("a", moment).model_copy(update={"p_neutral": 0.9}))
    assert not has_valid_sentiment(
        article("a", moment).model_copy(update={"p_neutral": None, "p_positive": 0.95})
    )


def test_timestamp_quality_levels():
    assert timestamp_quality(article("a", datetime(2024, 3, 4, 15, tzinfo=UTC))) is (
        TimestampQuality.FULL
    )
    assert timestamp_quality(article("a", None, published_date=date(2024, 3, 4))) is (
        TimestampQuality.DATE_ONLY
    )
    assert timestamp_quality(article("a", datetime(2024, 3, 4, 15))) is TimestampQuality.DATE_ONLY
    assert timestamp_quality(article("a", None)) is TimestampQuality.UNUSABLE


def test_title_normalisation_is_deterministic_and_strips_only_the_own_publisher():
    assert normalize_title("Acme Beats Estimates! - Reuters", "Reuters") == "acme beats estimates"
    assert normalize_title("  acme   beats estimates | reuters ", "Reuters") == (
        "acme beats estimates"
    )
    # A trailing segment that is not the article's own source is headline content.
    assert normalize_title("Acme - A New Era", "Reuters") == "acme a new era"
    assert normalize_title("Reuters", "Reuters") == "reuters"


def test_title_and_canonical_deduplication_within_a_session():
    moment = datetime(2024, 3, 4, 15, tzinfo=UTC)
    articles = [
        article("a", moment, title="Acme beats estimates - Reuters", source="Reuters"),
        article(
            "b",
            moment + timedelta(minutes=5),
            title="ACME beats estimates",
            source="Yahoo",
            positive=0.2,
            negative=0.6,
        ),
        article("c", moment, url="https://example.com/a?utm_source=x", source="Mirror"),
        article("a", moment + timedelta(minutes=9), source="Replay"),
        article("d", moment, title="Acme guidance raised", source="CNBC", positive=0.4),
    ]
    forward = build_session_signals(articles, "ACME", US)
    backward = build_session_signals(list(reversed(articles)), "ACME", US)
    assert forward.signals == backward.signals
    (signal,) = forward.signals
    assert signal.article_ids == ("a", "d")
    assert signal.article_count == 2
    assert signal.assigned_article_count == 3
    assert signal.distinct_source_count == 2
    # The duplicate's polarity does not enter the mean: (0.7 + 0.3) / 2.
    assert signal.signal == pytest.approx(0.5)
    assert forward.diagnostics.canonical_duplicates == 2
    assert forward.diagnostics.title_duplicates == 1


def test_same_title_in_different_sessions_is_not_deduplicated():
    articles = [
        article("a", datetime(2024, 3, 4, 15, tzinfo=UTC), title="Acme shares slide"),
        article("b", datetime(2024, 3, 5, 15, tzinfo=UTC), title="Acme shares slide"),
    ]
    assert len(build_session_signals(articles, "ACME", US).signals) == 2


def test_session_signal_is_a_mean_not_a_count_weighted_sum():
    moment = datetime(2024, 3, 4, 15, tzinfo=UTC)
    few = [article("a", moment, positive=0.6, negative=0.1)]
    many = [article(f"m{n}", moment, positive=0.6, negative=0.1) for n in range(9)]
    assert build_session_signals(few, "ACME", US).signals[0].signal == pytest.approx(
        build_session_signals(many, "ACME", US).signals[0].signal
    )


def test_no_news_session_is_missing_not_zero():
    articles = [
        article("a", datetime(2024, 3, 4, 15, tzinfo=UTC)),
        article("b", datetime(2024, 3, 6, 15, tzinfo=UTC)),
    ]
    signals = build_session_signals(articles, "ACME", US).signals
    assert [signal.session for signal in signals] == [date(2024, 3, 4), date(2024, 3, 6)]
    assert all(signal.signal != 0 for signal in signals)


def test_ineligible_articles_are_counted_and_excluded():
    moment = datetime(2024, 3, 4, 15, tzinfo=UTC)
    articles = [
        article("ok", moment),
        article("other", moment, ticker="ZZZ"),
        article("demo", moment, is_demo=True),
        article("bad", moment).model_copy(update={"p_negative": 1.5}),
        article("untimed", None),
        article("dated", None, published_date=date(2024, 3, 4)),
    ]
    built = build_session_signals(articles, "acme", US)
    assert [signal.article_ids for signal in built.signals] == [("ok",), ("dated",)]
    assert built.signals[1].session == date(2024, 3, 5)
    diagnostics = built.diagnostics
    assert (diagnostics.wrong_ticker, diagnostics.demo) == (1, 1)
    assert (diagnostics.invalid_sentiment, diagnostics.unusable_timestamp) == (1, 1)
    assert (diagnostics.full_timestamp, diagnostics.date_only_timestamp) == (1, 1)


def test_scored_article_adapter_treats_exact_midnight_as_date_only():
    base = {
        "fingerprint": "f1",
        "ticker": "ACME",
        "title": "Acme rises",
        "url": "https://example.com/1",
        "source": "Reuters",
        "fetched_at": datetime(2024, 3, 5, tzinfo=UTC),
        "provider": "test",
        "relevance_score": 0.9,
        "label": "positive",
        "positive": 0.7,
        "negative": 0.1,
        "neutral": 0.2,
        "sentiment_score": 0.6,
        "model_name": "finbert",
        "scored_at": datetime(2024, 3, 5, tzinfo=UTC),
    }
    timed = from_scored_article(
        ScoredArticle(**base, published_at=datetime(2024, 3, 4, 15, 30, tzinfo=UTC))
    )
    dated = from_scored_article(
        ScoredArticle(**base, published_at=datetime(2024, 3, 4, tzinfo=UTC))
    )
    assert timed.article_id == "f1" and timed.published_at is not None
    assert (dated.published_at, dated.published_date) == (None, date(2024, 3, 4))
    assert (timed.p_positive, timed.p_negative) == (0.7, 0.1)


def test_google_news_pacific_midnight_stamp_is_date_only_in_both_dst_states():
    # The historical-range feed writes 00:00 America/Los_Angeles: 07:00 UTC in PDT, 08:00 in PST.
    assert split_publication_stamp(datetime(2025, 10, 27, 7, 0, tzinfo=UTC)) == (
        None,
        date(2025, 10, 27),
    )
    assert split_publication_stamp(datetime(2025, 11, 12, 8, 0, tzinfo=UTC)) == (
        None,
        date(2025, 11, 12),
    )
    # The same wall-clock hour in the other DST state is an ordinary instant.
    winter_seven = datetime(2025, 11, 12, 7, 0, tzinfo=UTC)
    assert split_publication_stamp(winter_seven) == (winter_seven, None)
    assert split_publication_stamp(datetime(2025, 11, 12, 7, 0)) == (None, date(2025, 11, 12))


def test_articles_without_a_url_are_deduplicated_by_id_and_title_only():
    moment = datetime(2024, 3, 4, 15, tzinfo=UTC)
    articles = [article(name, moment).model_copy(update={"url": None}) for name in ("a", "b", "a")]
    built = build_session_signals(articles, "ACME", US)
    assert built.signals[0].article_ids == ("a", "b")
    assert built.diagnostics.canonical_duplicates == 1


# --- regimes and thresholds ------------------------------------------------------------------


def signals_for(*articles_groups) -> list:
    combined = [item for group in articles_groups for item in group]
    return list(build_session_signals(combined, "ACME", US).signals)


def signal_at(position: int, *, positive: float, negative: float, sources: int = 3):
    (signal,) = build_session_signals(
        session_articles(position, positive=positive, negative=negative, sources=sources),
        "ACME",
        US,
    ).signals
    return signal


def test_regime_needs_threshold_and_three_distinct_sources():
    broad, narrow = signals_for(session_articles(ORIGIN), session_articles(ORIGIN + 1, sources=2))
    assert classify_regime(broad, THRESHOLDS) is Regime.CLEARLY_POSITIVE
    assert classify_regime(narrow, THRESHOLDS) is None
    assert classify_regime(broad, RegimeThresholds(negative=0.30, positive=0.95)) is None
    negative = signal_at(ORIGIN, positive=0.1, negative=0.8)
    assert classify_regime(negative, THRESHOLDS) is Regime.CLEARLY_NEGATIVE
    # The boundary is inclusive on both sides.
    inclusive = RegimeThresholds(negative=-negative.signal, positive=broad.signal)
    assert classify_regime(broad, inclusive) is Regime.CLEARLY_POSITIVE
    assert classify_regime(negative, inclusive) is Regime.CLEARLY_NEGATIVE


def test_each_regime_uses_its_own_threshold():
    # A signal of +0.35 enters the positive regime while -0.35 misses the stricter negative one.
    thresholds = RegimeThresholds(negative=0.50, positive=0.30)
    positive = signal_at(ORIGIN, positive=0.55, negative=0.20)
    negative = signal_at(ORIGIN, positive=0.20, negative=0.55)
    assert positive.signal == pytest.approx(0.35)
    assert classify_regime(positive, thresholds) is Regime.CLEARLY_POSITIVE
    assert classify_regime(negative, thresholds) is None
    assert classify_regime(signal_at(ORIGIN, positive=0.1, negative=0.8), thresholds) is (
        Regime.CLEARLY_NEGATIVE
    )


def test_asymmetric_threshold_selection_cuts_each_tail_on_its_own_side():
    # Deliberately skewed: the positive tail is wide and the negative tail is narrow.
    pooled = [-0.30 + 0.01 * number for number in range(41)] + [
        0.50 + 0.01 * number for number in range(9)
    ]
    signals = [
        signal_at(ORIGIN + number, positive=0.5 + value / 2, negative=0.5 - value / 2)
        for number, value in enumerate(pooled)
    ]
    selection = select_thresholds(signals)
    observed = sorted(signal.signal for signal in signals)
    assert selection.population_count == len(pooled)
    assert selection.positive_quantile == 0.85
    assert selection.negative_quantile == 0.15
    assert selection.raw_positive == pytest.approx(round(np.quantile(observed, 0.85), 2))
    assert selection.raw_negative == pytest.approx(round(-np.quantile(observed, 0.15), 2))
    assert selection.thresholds.positive != selection.thresholds.negative
    assert selection.methodology_version == METHODOLOGY_VERSION
    # Thresholds are quotable two-decimal numbers, not raw floats.
    for value in (selection.thresholds.positive, selection.thresholds.negative):
        assert value == round(value, 2)


def test_threshold_selection_population_is_sentiment_only_and_source_gated():
    # Extreme sessions carried by too few publishers cannot shape either tail.
    qualified = [signal_at(ORIGIN + number, positive=0.55, negative=0.20) for number in range(10)]
    thin = [
        signal_at(ORIGIN + 20 + number, positive=0.99, negative=0.0, sources=2)
        for number in range(10)
    ]
    assert selection_population(qualified + thin) == [signal.signal for signal in qualified]
    selection = select_thresholds(qualified + thin)
    assert (selection.population_count, selection.pooled_session_count) == (10, 20)
    assert selection.thresholds == select_thresholds(qualified).thresholds
    # The signature admits session signals only: there is no parameter a return could arrive in.
    parameters = set(inspect.signature(select_thresholds).parameters)
    assert parameters == {"signals", "min_distinct_sources"}


def test_each_threshold_floor_applies_independently():
    # A pool whose positive tail clears the floor while its negative tail does not.
    pooled = [-0.02 * number for number in range(10)] + [0.05 * number for number in range(11)]
    signals = [
        signal_at(ORIGIN + number, positive=0.5 + value / 2, negative=0.5 - value / 2)
        for number, value in enumerate(pooled)
    ]
    selection = select_thresholds(signals)
    assert selection.raw_negative < 0.20 <= selection.raw_positive
    assert selection.thresholds.negative == 0.20
    assert selection.negative_floor_applied
    assert not selection.positive_floor_applied
    assert selection.thresholds.positive == selection.raw_positive
    # Flooring makes that regime rarer; it is never widened to recover events.
    assert selection.negative_tail_share <= selection.positive_tail_share


def test_threshold_selection_with_an_empty_population_falls_back_to_the_floor():
    selection = select_thresholds([])
    assert selection.thresholds == RegimeThresholds(negative=0.20, positive=0.20)
    assert selection.positive_floor_applied and selection.negative_floor_applied
    assert (selection.population_count, selection.pooled_session_count) == (0, 0)


def test_thresholds_can_never_be_constructed_below_the_floor():
    with pytest.raises(ValidationError):
        RegimeThresholds(negative=0.19, positive=0.30)
    with pytest.raises(ValidationError):
        RegimeThresholds(negative=0.30, positive=0.0)


def test_threshold_interface_requires_explicit_provisional_values_until_frozen():
    with pytest.raises(ValueError, match="not frozen"):
        resolve_thresholds(None)
    provisional = RegimeThresholds(negative=0.25, positive=0.35)
    assert resolve_thresholds(provisional) == (provisional, ThresholdStatus.PROVISIONAL)


def test_frozen_thresholds_are_used_and_reported(monkeypatch):
    frozen = RegimeThresholds(negative=0.35, positive=0.40)
    monkeypatch.setattr("marketsentinel.market_reaction.engine.MR_V1_FROZEN_THRESHOLDS", frozen)
    assert resolve_thresholds(None) == (frozen, ThresholdStatus.FROZEN)
    other = RegimeThresholds(negative=0.40, positive=0.40)
    assert resolve_thresholds(other) == (other, ThresholdStatus.PROVISIONAL)


# --- returns ---------------------------------------------------------------------------------


def test_adjusted_and_benchmark_adjusted_return_arithmetic():
    stock = flat_prices(ORIGIN - 1, ORIGIN + 11)
    benchmark = flat_prices(ORIGIN - 1, ORIGIN + 11, 4000.0)
    stock[ORIGIN - 1], stock[ORIGIN], stock[ORIGIN + 5] = 96.0, 100.0, 110.0
    benchmark[ORIGIN - 1], benchmark[ORIGIN + 5] = 3960.0, 4080.0
    path = reaction_path(
        align_prices(observations(stock), US), align_prices(observations(benchmark), US), ORIGIN
    )
    assert path.complete
    assert path.stock[5] == pytest.approx(0.10)
    assert path.benchmark[5] == pytest.approx(0.02)
    assert path.market_adjusted[5] == pytest.approx(0.08)
    # Horizon 0 is the news session's own close-to-close move.
    assert path.stock[0] == pytest.approx(100 / 96 - 1)
    assert path.market_adjusted[0] == pytest.approx((100 / 96 - 1) - (4000 / 3960 - 1))
    assert path.market_adjusted[1] == pytest.approx(0.0)


def test_price_alignment_drops_invalid_and_off_calendar_observations():
    prices = [
        PriceObservation(date=date(2024, 3, 4), adjusted_close=100.0),
        PriceObservation(date=date(2024, 3, 5), adjusted_close=float("nan")),
        PriceObservation(date=date(2024, 3, 6), adjusted_close=0.0),
        PriceObservation(date=date(2024, 3, 9), adjusted_close=101.0),
        PriceObservation(date=date(2024, 3, 8), adjusted_close=102.0),
    ]
    aligned = align_prices(prices, US)
    assert len(aligned.closes) == 2
    assert (aligned.invalid_observations, aligned.off_calendar_dates) == (2, 1)
    assert aligned.gap_sessions() == 3


def test_missing_stock_or_benchmark_price_is_never_imputed():
    articles, stock, benchmark = scenario([0.02])
    missing_stock = [item for item in stock if item.date != US.sessions[ORIGIN + 5]]
    result = run(articles, missing_stock, benchmark)
    (event,) = result.positive.events
    assert event.status is EventStatus.MISSING_PRICE_DATA
    assert event.market_adjusted_returns[5] is None
    assert event.market_adjusted_returns[4] == pytest.approx(0.02)
    assert event.primary_market_adjusted_return is None
    assert result.positive.resolved_event_count == 0
    assert result.data_quality.stock_price_gap_sessions == 1

    missing_benchmark = [item for item in benchmark if item.date != US.sessions[ORIGIN + 3]]
    result = run(articles, stock, missing_benchmark)
    assert result.positive.events[0].status is EventStatus.MISSING_PRICE_DATA
    assert result.data_quality.benchmark_price_gap_sessions == 1
    assert result.data_quality.unresolved_event_windows == 1


def test_complete_path_is_required_and_recent_events_are_pending():
    articles, stock, benchmark = scenario([0.02, 0.03])
    last_needed = US.sessions[ORIGIN + 12 + 10]
    truncated_stock = [item for item in stock if item.date < last_needed]
    result = run(articles, truncated_stock, benchmark)
    first, second = result.positive.events
    assert first.status is EventStatus.RESOLVED
    assert len(first.market_adjusted_returns) == 11
    assert second.status is EventStatus.PENDING
    # +5 is known for the pending event, yet it stays out of the resolved statistics.
    assert second.market_adjusted_returns[5] == pytest.approx(0.03)
    assert result.positive.primary.n == 1
    assert result.positive.pending_event_count == 1


def test_event_without_a_prior_close_is_unresolved():
    articles, stock, benchmark = scenario([0.02])
    result = run(articles, [item for item in stock if item.date >= US.sessions[ORIGIN]], benchmark)
    assert result.positive.events[0].status is EventStatus.MISSING_PRICE_DATA
    assert result.positive.events[0].market_adjusted_returns[0] is None


# --- exclusivity -----------------------------------------------------------------------------


def test_same_regime_window_exclusivity():
    offsets = [0, 2, 4, 5, 7, 10]
    articles = [item for offset in offsets for item in session_articles(ORIGIN + offset)]
    prices = observations(flat_prices(ORIGIN - 1, ORIGIN + 40))
    result = run(articles, prices, prices)
    statuses = {
        US.index_of(event.session) - ORIGIN: event.status for event in result.positive.events
    }
    assert statuses == {
        0: EventStatus.RESOLVED,
        2: EventStatus.SUPPRESSED_OVERLAP,
        4: EventStatus.SUPPRESSED_OVERLAP,
        # (t, t+5] and (t+5, t+10] share no session, so +5 is the first admissible event.
        5: EventStatus.RESOLVED,
        7: EventStatus.SUPPRESSED_OVERLAP,
        10: EventStatus.RESOLVED,
    }
    suppressed = result.positive.events[1]
    assert suppressed.suppressed_by == US.sessions[ORIGIN]
    assert result.positive.suppressed_event_count == 3
    assert result.positive.qualifying_event_count == 6


def test_cross_regime_events_do_not_suppress_each_other():
    articles = session_articles(ORIGIN) + session_articles(ORIGIN + 2, positive=0.1, negative=0.8)
    prices = observations(flat_prices(ORIGIN - 1, ORIGIN + 40))
    result = run(articles, prices, prices)
    assert result.positive.events[0].status is EventStatus.RESOLVED
    assert result.negative.events[0].status is EventStatus.RESOLVED


# --- statistics ------------------------------------------------------------------------------


def test_bootstrap_is_deterministic_and_seeded_from_the_methodology_version():
    values = [0.01, -0.02, 0.03, 0.015, -0.005, 0.02, 0.04, -0.01, 0.0, 0.025]
    seed = bootstrap_seed("clearly_positive", "h5")
    assert bootstrap_mean_ci(values, seed) == bootstrap_mean_ci(values, seed)
    assert seed == bootstrap_seed("clearly_positive", "h5")
    assert seed != bootstrap_seed("clearly_negative", "h5")
    assert seed != bootstrap_seed("clearly_positive", "h5", methodology_version="mr-v2")
    low, high = bootstrap_mean_ci(values, seed)
    assert low < float(np.mean(values)) < high
    # Pinned so an accidental change to the seed derivation or resampling scheme is caught.
    assert (low, high) == pytest.approx((-0.0005, 0.0215), abs=2e-3)
    with pytest.raises(ValueError):
        bootstrap_mean_ci([], seed)


def test_summary_statistics():
    summary = summarize_returns([0.02, -0.01, 0.03, 0.0], seed=1)
    assert summary.n == 4
    assert summary.mean == pytest.approx(0.01)
    assert summary.median == pytest.approx(0.01)
    assert summary.share_positive == pytest.approx(0.5)
    assert split_half_means([0.01, 0.03, -0.02, -0.04, 0.09]) == pytest.approx((0.02, 0.01))


def test_spearman_is_descriptive_and_gated_on_sample_size():
    signals = [value / 100 for value in range(120)]
    assert spearman(signals[:99], signals[:99]) is None
    assert spearman(signals, signals).rho == pytest.approx(1.0)
    assert spearman(signals, [-value for value in signals]).rho == pytest.approx(-1.0)
    assert spearman(signals, [1.0] * 120) is None
    tied = spearman([0.0, 0.0, 1.0, 1.0], [1.0, 2.0, 3.0, 4.0], minimum=4)
    assert tied.rho == pytest.approx(0.8944, abs=1e-4)


# --- evidence states -------------------------------------------------------------------------


def noisy(center: float, count: int, spread: float = 0.004) -> list[float]:
    return [center + spread * ((number % 5) - 2) for number in range(count)]


def test_insufficient_events_state_hides_statistics_from_a_verdict():
    result = run(*scenario(noisy(0.03, 9), spacing=20))
    assert result.state is None
    assert result.positive.state is EvidenceState.INSUFFICIENT_EVENTS
    assert result.negative.state is EvidenceState.INSUFFICIENT_EVENTS
    assert result.negative.primary is None


def test_preliminary_state():
    result = run(*scenario(noisy(0.03, 19)))
    assert result.positive.primary.n == 19
    assert result.positive.state is EvidenceState.PRELIMINARY


def test_detected_state_and_reaction_path():
    result = run(*scenario(noisy(0.03, 24)))
    positive = result.positive
    assert positive.state is EvidenceState.DETECTED
    assert positive.primary.n == 24
    assert positive.primary.mean == pytest.approx(0.0298, abs=1e-3)
    assert positive.primary.ci_low > 0
    assert positive.primary.share_positive == 1.0
    assert all(
        flag is True
        for flag in (
            positive.ci_excludes_zero,
            positive.meets_minimum_effect,
            positive.split_half_same_sign,
            positive.mean_median_same_sign,
        )
    )
    assert [point.horizon for point in positive.path] == list(range(11))
    assert [point.contemporaneous for point in positive.path] == [True] + [False] * 10
    assert positive.point_at(0).statistics.mean == pytest.approx(0.0)
    assert positive.point_at(5).statistics == positive.primary
    # Every event here has exact timing, so both cohorts cover the same 24 events -- and each
    # horizon still states which cohort it used rather than leaving it to be assumed.
    assert positive.point_at(0).cohort is PathCohort.EXACT_TIMING
    assert positive.point_at(1).cohort is PathCohort.ALL_RESOLVED
    assert positive.day_zero_event_count == positive.resolved_event_count == 24
    assert positive.point_at(0).statistics.n == 24


def test_identical_returns_never_reach_a_verdict_through_a_zero_width_interval():
    # 25 identical +1% returns gave `detected` on main (zero-width percentile interval).
    result = run(*scenario([0.01] * 25))
    positive = result.positive
    assert positive.primary.n == 25
    assert positive.primary.interval_method is IntervalMethod.STUDENT_T
    assert positive.primary.interval_degenerate is True
    assert positive.ci_excludes_zero is False
    assert positive.state is EvidenceState.NO_CONSISTENT_RELATIONSHIP
    assert all(point.statistics.interval_degenerate for point in positive.path[1:])
    assert result.bootstrap_resamples is None


def test_detected_state_for_the_negative_regime():
    result = run(*scenario(noisy(-0.03, 22), positive=False))
    assert result.negative.state is EvidenceState.DETECTED
    assert result.negative.primary.ci_high < 0
    assert result.positive.state is EvidenceState.INSUFFICIENT_EVENTS


def test_no_consistent_relationship_when_the_interval_spans_zero():
    returns = [0.03 if number % 2 else -0.03 for number in range(24)]
    result = run(*scenario(returns))
    assert result.positive.state is EvidenceState.NO_CONSISTENT_RELATIONSHIP
    assert result.positive.ci_excludes_zero is False


def test_no_consistent_relationship_when_the_effect_is_below_half_a_percent():
    result = run(*scenario(noisy(0.003, 24, spread=0.0005)))
    assert result.positive.ci_excludes_zero is True
    assert result.positive.meets_minimum_effect is False
    assert result.positive.state is EvidenceState.NO_CONSISTENT_RELATIONSHIP


def test_split_half_failure_is_unstable():
    returns = noisy(-0.004, 12, spread=0.001) + noisy(0.06, 12)
    result = run(*scenario(returns))
    positive = result.positive
    assert positive.ci_excludes_zero and positive.meets_minimum_effect
    assert positive.first_half_mean < 0 < positive.second_half_mean
    assert positive.split_half_same_sign is False
    assert positive.state is EvidenceState.UNSTABLE


def test_mean_median_sign_disagreement_is_unstable():
    # A few large gains, spread across both halves, outweigh a slightly negative typical event.
    returns = [0.20 if number % 4 == 0 else -0.002 for number in range(24)]
    result = run(*scenario(returns))
    positive = result.positive
    assert positive.primary.mean > 0 > positive.primary.median
    assert positive.ci_excludes_zero and positive.split_half_same_sign
    assert positive.mean_median_same_sign is False
    assert positive.state is EvidenceState.UNSTABLE


def test_regime_state_rules_directly():
    strong = summarize_returns(noisy(0.03, 20), seed=1)
    assert regime_state(None, None, None) is EvidenceState.INSUFFICIENT_EVENTS
    assert regime_state(strong, 0.03, 0.03) is EvidenceState.DETECTED
    assert regime_state(strong, 0.03, 0.0) is EvidenceState.UNSTABLE
    assert regime_state(strong, None, None) is EvidenceState.UNSTABLE


def test_not_enough_history_overrides_regime_states():
    result = run(*scenario(noisy(0.03, 24), spacing=5))
    assert result.history.span_sessions == 115
    assert not result.history.meets_span
    assert result.state is EvidenceState.NOT_ENOUGH_HISTORY
    assert result.positive.state is EvidenceState.NOT_ENOUGH_HISTORY
    # The statistics stay inspectable even though no verdict is offered.
    assert result.positive.primary.n == 24


# --- history sufficiency ---------------------------------------------------------------------


HISTORY_PRICES = observations(flat_prices(ORIGIN - 1, ORIGIN + 200))


def history_run(positions, *, thin: set[int] | None = None):
    """Build neutral signal sessions at exactly these offsets; `thin` ones carry only 2 sources."""

    thin = thin or set()
    articles = [
        item
        for offset in positions
        for item in session_articles(
            ORIGIN + offset, positive=0.45, negative=0.45, sources=2 if offset in thin else 3
        )
    ]
    return run(articles, HISTORY_PRICES, HISTORY_PRICES).history


def test_history_requires_span_density_and_qualified_count_together():
    history = history_run(range(0, 127))
    assert (history.span_sessions, history.signal_session_count) == (126, 127)
    assert history.qualified_signal_session_count == 127
    assert history.signal_density == pytest.approx(1.0)
    assert (history.meets_span, history.meets_density, history.meets_qualified_count) == (
        True,
        True,
        True,
    )
    assert history.sufficient
    assert (history.min_span_sessions, history.min_signal_density) == (126, 0.50)
    assert history.min_qualified_signal_sessions == 63


def test_span_boundary_is_exactly_126_sessions():
    assert history_run(range(0, 127)).meets_span
    below = history_run(range(0, 126))
    assert below.span_sessions == 125
    assert not below.meets_span
    assert not below.sufficient
    # Only the span fails; the other two are still satisfied.
    assert below.meets_density and below.meets_qualified_count


def test_density_boundary_is_exactly_one_half():
    # 64 sessions inside a 128-session window: density is exactly 0.50 and passes.
    positions = [0, *range(2, 126, 2), 127]
    assert len(positions) == 64
    exact = history_run(positions)
    assert (exact.span_sessions, exact.signal_session_count) == (127, 64)
    assert exact.signal_density == pytest.approx(0.50)
    assert exact.meets_density and exact.sufficient
    # One session fewer in the same window drops below the floor.
    below = history_run([0, *range(4, 126, 2), 127])
    assert below.signal_session_count == 63
    assert below.signal_density == pytest.approx(63 / 128)
    assert not below.meets_density
    assert not below.sufficient
    # Span and qualified count both still pass, so density alone decided it.
    assert below.meets_span and below.meets_qualified_count


def test_qualified_session_boundary_is_exactly_63():
    # A dense, long window where most sessions are carried by too few publishers to qualify.
    thin = {offset for offset in range(128) if offset % 2 == 1 or offset > 124}
    exact = history_run(range(0, 128), thin=thin)
    assert exact.qualified_signal_session_count == 63
    assert exact.meets_qualified_count and exact.sufficient
    below = history_run(range(0, 128), thin=thin | {0})
    assert below.qualified_signal_session_count == 62
    assert not below.meets_qualified_count
    assert not below.sufficient
    # Span and density both still pass, so the qualified count alone decided it.
    assert below.meets_span and below.meets_density


def test_sparse_disjoint_history_fails_although_its_span_is_wide():
    # Two dense clusters a year apart: a span-only rule would have called this enough history.
    positions = [*range(0, 25), *range(150, 175)]
    history = history_run(positions)
    assert history.span_sessions == 174
    assert history.meets_span
    assert history.signal_session_count == 50
    assert history.signal_density == pytest.approx(50 / 175)
    assert not history.meets_density
    assert not history.meets_qualified_count
    assert not history.sufficient


def test_history_failure_is_reported_at_company_level_with_statistics_intact():
    articles, stock, benchmark = scenario(noisy(0.03, 24), dense=False)
    result = run(articles, stock, benchmark)
    assert result.history.meets_span
    assert not result.history.meets_density
    assert result.state is EvidenceState.NOT_ENOUGH_HISTORY
    assert result.positive.state is EvidenceState.NOT_ENOUGH_HISTORY
    assert result.positive.primary.n == 24
    # The same events with the intervening sessions covered clear the rule.
    dense = run(*scenario(noisy(0.03, 24)))
    assert dense.history.sufficient
    assert dense.positive.state is EvidenceState.DETECTED


def test_data_quality_inadequate_states():
    articles, stock, benchmark = scenario(noisy(0.03, 24))
    unresolved = analyze_market_reaction(
        ticker="ACME",
        listing_market="Nikkei 225",
        articles=articles,
        stock_prices=stock,
        benchmark_prices=benchmark,
        thresholds=THRESHOLDS,
    )
    assert unresolved.state is EvidenceState.DATA_QUALITY_INADEQUATE
    assert unresolved.data_quality.reasons == ("unresolved_exchange",)
    assert not unresolved.data_quality.exchange_resolved
    assert unresolved.methodology_version == METHODOLOGY_VERSION

    no_benchmark = run(articles, stock, [])
    assert no_benchmark.state is EvidenceState.DATA_QUALITY_INADEQUATE
    assert no_benchmark.positive.state is EvidenceState.DATA_QUALITY_INADEQUATE
    assert "no_usable_benchmark_prices" in no_benchmark.data_quality.reasons

    untimed = [article(f"u{number}", None) for number in range(30)]
    strict = run(
        articles + untimed,
        stock,
        benchmark,
        quality_policy=DataQualityPolicy(max_unusable_timestamp_share=0.01),
    )
    quality = strict.data_quality
    assert quality.ineligible_unusable_timestamp == 30
    assert quality.unusable_timestamp_share > 0.01
    assert quality.unusable_timestamp_share == pytest.approx(
        30 / (30 + quality.full_timestamp_count + quality.date_only_timestamp_count)
    )
    assert quality.reasons == ("unusable_timestamp_share_above_ceiling",)
    assert strict.state is EvidenceState.DATA_QUALITY_INADEQUATE
    # Without a supplied ceiling the share is reported but no threshold is invented.
    assert run(articles + untimed, stock, benchmark).state is None


# --- timing class and the day-0 cohort ---------------------------------------------------------


def test_timing_class_and_date_only_share_on_session_signals():
    exact = signal_at(ORIGIN, positive=0.8, negative=0.1)
    assert (exact.date_only_share, exact.timing_class) == (0.0, TimingClass.EXACT)
    (lagged,) = build_session_signals(session_articles(ORIGIN, date_only=True), "ACME", US).signals
    assert (lagged.date_only_share, lagged.timing_class) == (1.0, TimingClass.LAGGED)
    # One date-only article is enough: the session's timing is no longer trustworthy.
    mixed_articles = session_articles(ORIGIN, sources=2) + session_articles(
        ORIGIN, date_only=True, sources=1
    )
    (mixed,) = build_session_signals(mixed_articles, "ACME", US).signals
    assert mixed.article_count == 3
    assert mixed.date_only_share == pytest.approx(1 / 3)
    assert mixed.timing_class is TimingClass.LAGGED


def test_timing_metadata_reaches_the_event_and_the_quality_report():
    result = run(*scenario(noisy(0.03, 24), date_only=True))
    event = result.positive.events[0]
    assert event.timing_class is TimingClass.LAGGED
    assert event.date_only_share == 1.0
    assert result.positive.exact_timing_event_count == 0
    assert result.data_quality.lagged_signal_session_count == 24
    # Provenance survives the conservative roll-forward.
    assert len(event.article_ids) == 3
    assert all(article_id.startswith("d") for article_id in event.article_ids)


def test_lagged_events_are_excluded_from_day_zero_but_kept_for_one_through_ten():
    lagged = run(*scenario(noisy(0.03, 24), date_only=True)).positive
    assert lagged.resolved_event_count == 24
    # No exact event, so there is no day-0 aggregate at all rather than a misleading one.
    assert lagged.day_zero_event_count == 0
    assert lagged.point_at(0) is None
    assert [point.horizon for point in lagged.path] == list(range(1, 11))
    # The forward horizons still use every resolved event, lagged included.
    assert lagged.point_at(5).statistics.n == 24
    assert lagged.point_at(5).cohort is PathCohort.ALL_RESOLVED
    assert lagged.primary.n == 24


def test_day_zero_uses_only_exact_events_while_forward_horizons_use_all():
    returns = noisy(0.03, 24)
    articles, stock, benchmark = scenario(returns)
    lagged_positions = {ORIGIN + number * 12 for number in range(10)}
    # Re-stamp the first ten events as date-only; their sessions are unchanged.
    mixed = [item for item in articles if not _is_event_article(item, lagged_positions)] + [
        item
        for position in sorted(lagged_positions)
        for item in session_articles(position, positive=0.8, negative=0.1, date_only=True)
    ]
    regime = run(mixed, stock, benchmark).positive
    assert regime.resolved_event_count == 24
    assert regime.day_zero_event_count == 14
    day_zero = regime.point_at(0)
    assert day_zero.cohort is PathCohort.EXACT_TIMING
    assert day_zero.statistics.n == 14
    # +1..+10 keep the full cohort, so the sample sizes differ and both are stated.
    for horizon in range(1, 11):
        point = regime.point_at(horizon)
        assert point.cohort is PathCohort.ALL_RESOLVED
        assert point.statistics.n == 24
    assert day_zero.statistics.n != regime.point_at(1).statistics.n
    assert regime.primary.n == 24


def _is_event_article(item, positions) -> bool:
    return any(item.article_id.startswith(f"s{position}-") for position in positions)


def test_day_zero_cohort_uses_its_own_bootstrap_stream():
    regime = run(*scenario(noisy(0.03, 24))).positive
    assert regime.point_at(0).statistics.ci_low != regime.point_at(1).statistics.ci_low


# --- integrity -------------------------------------------------------------------------------


def test_extreme_valid_move_is_flagged_but_retained():
    returns = noisy(0.03, 23) + [0.45]
    result = run(*scenario(returns))
    extreme = result.positive.events[-1]
    assert extreme.status is EventStatus.RESOLVED
    assert extreme.integrity_flags == ("extreme_stock_session_move:+1",)
    assert extreme.primary_market_adjusted_return == pytest.approx(0.45)
    assert result.positive.primary.n == 24
    assert result.data_quality.integrity_review_candidates == 1


def test_event_is_excluded_only_with_recorded_evidence():
    articles, stock, benchmark = scenario(noisy(0.03, 23) + [0.45])
    session = US.sessions[ORIGIN + 23 * 12]
    result = run(
        articles, stock, benchmark, integrity_exclusions={session: "unadjusted 2-for-1 split"}
    )
    excluded = result.positive.events[-1]
    assert excluded.status is EventStatus.EXCLUDED_INVALID
    assert excluded.exclusion_reason == "unadjusted 2-for-1 split"
    assert result.positive.primary.n == 23
    assert result.positive.excluded_event_count == 1


def test_extreme_benchmark_move_is_flagged():
    articles, stock, benchmark = scenario([0.02])
    crashed = [
        item.model_copy(update={"adjusted_close": 90.0})
        if item.date >= US.sessions[ORIGIN + 3]
        else item
        for item in benchmark
    ]
    event = run(articles, stock, crashed).positive.events[0]
    assert event.integrity_flags == ("extreme_benchmark_session_move:+3",)
    assert event.status is EventStatus.RESOLVED


# --- result contract -------------------------------------------------------------------------


def test_result_carries_version_provenance_and_round_trips():
    result = run(*scenario(noisy(0.03, 24)))
    assert result.methodology_version == "mr-v1"
    assert result.title_normalization_version == "mr-title-v1"
    assert (result.thresholds, result.threshold_status) == (
        THRESHOLDS,
        ThresholdStatus.PROVISIONAL,
    )
    assert (result.positive.threshold, result.negative.threshold) == (0.30, 0.30)
    assert (result.exchange, result.benchmark_symbol) == (ListingExchange.US, "SPY")
    assert (result.primary_horizon, result.max_path_horizon) == (5, 10)
    assert (result.min_distinct_sources, result.bootstrap_resamples) == (3, None)
    event = result.positive.events[0]
    assert event.article_ids == (f"s{ORIGIN}-0", f"s{ORIGIN}-1", f"s{ORIGIN}-2")
    assert len(event.sources) == event.distinct_source_count == 3
    assert result.session_signals[0].article_ids == event.article_ids
    assert type(result).model_validate_json(result.model_dump_json()) == result


def test_engine_is_deterministic_and_input_order_independent():
    articles, stock, benchmark = scenario(noisy(0.03, 24))
    first = run(articles, stock, benchmark)
    second = run(list(reversed(articles)), list(reversed(stock)), list(reversed(benchmark)))
    assert first == second


def test_engine_builds_its_own_calendar_for_a_london_listing():
    calendar_articles = [
        article(f"l{number}", datetime(2024, 12, 24, 13, 0, tzinfo=UTC)) for number in range(3)
    ]
    sessions = [
        session for session in UK.sessions if date(2024, 12, 2) <= session <= date(2025, 1, 24)
    ]
    prices = [PriceObservation(date=session, adjusted_close=100.0) for session in sessions]
    result = analyze_market_reaction(
        ticker="ACME",
        listing_market="FTSE 100",
        articles=calendar_articles,
        stock_prices=prices,
        benchmark_prices=prices,
        thresholds=THRESHOLDS,
    )
    assert (result.exchange, result.benchmark_symbol) == (ListingExchange.LONDON, "CUKX.L")
    (event,) = result.positive.events
    # Published after the Christmas Eve early close: first session is 27 December.
    assert event.session == date(2024, 12, 27)
    assert event.status is EventStatus.RESOLVED


def test_empty_inputs_produce_a_safe_typed_result():
    result = analyze_market_reaction(
        ticker="ACME",
        listing_market="S&P 500",
        articles=[],
        stock_prices=[],
        benchmark_prices=[],
        thresholds=THRESHOLDS,
    )
    assert result.state is EvidenceState.DATA_QUALITY_INADEQUATE
    assert result.signal_session_count == 0
    assert result.positive.events == ()


# --- synthetic null / placebo ----------------------------------------------------------------


def null_company(seed: int, sessions: int = 420):
    """Sentiment and prices drawn independently: by construction there is nothing to detect."""

    generator = np.random.default_rng(seed)
    stock = 100 * np.cumprod(1 + generator.normal(0, 0.02, sessions + 12))
    benchmark = 4000 * np.cumprod(1 + generator.normal(0, 0.01, sessions + 12))
    articles = []
    for offset in range(sessions):
        positive = float(generator.uniform(0.0, 0.9))
        articles += session_articles(
            ORIGIN + offset, positive=round(positive, 4), negative=round(0.9 - positive, 4)
        )
    positions = range(ORIGIN - 1, ORIGIN + sessions + 11)
    return (
        articles,
        observations(dict(zip(positions, stock.tolist(), strict=True))),
        observations(dict(zip(positions, benchmark.tolist(), strict=True))),
    )


def test_synthetic_null_rarely_produces_a_detected_relationship():
    regimes = detected = 0
    correlations = []
    for seed in range(30):
        result = run(*null_company(seed))
        for regime in (result.positive, result.negative):
            assert regime.resolved_event_count >= 20
            regimes += 1
            detected += regime.state is EvidenceState.DETECTED
        correlations.append(result.spearman.rho)
    # 60 independent null regimes. The measured rate is reported, not assumed; this bound only
    # guards against the procedure becoming grossly over-eager.
    assert detected / regimes <= 0.10
    assert abs(float(np.mean(correlations))) < 0.05


def planted_company(seed: int = 7, sessions: int = 420, spacing: int = 14):
    """Random-walk prices with a planted +0.6%/session drift over t+1..t+5 of each event.

    The drift is handed back over t+8..t+12, so the unconditional mean move stays zero and only
    the alignment of signal and sessions carries the relationship.
    """

    generator = np.random.default_rng(seed)
    stock_moves = generator.normal(0, 0.01, sessions + 12)
    benchmark_moves = generator.normal(0, 0.005, sessions + 12)
    event_offsets = list(range(0, sessions - spacing, spacing))
    for offset in event_offsets:
        # Index 0 of the move arrays is session ORIGIN - 1.
        stock_moves[offset + 2 : offset + 7] += 0.006
        stock_moves[offset + 9 : offset + 14] -= 0.006
    positions = range(ORIGIN - 1, ORIGIN + sessions + 11)
    stock = dict(zip(positions, (100 * np.cumprod(1 + stock_moves)).tolist(), strict=True))
    benchmark = dict(zip(positions, (4000 * np.cumprod(1 + benchmark_moves)).tolist(), strict=True))
    return event_offsets, observations(stock), observations(benchmark)


def events_on(offsets, sessions: int = 420):
    """Positive events at these offsets, with neutral news everywhere else for history density."""

    positions = {ORIGIN + offset for offset in offsets}
    articles = [item for position in sorted(positions) for item in session_articles(position)]
    return articles + filler_sessions(ORIGIN, ORIGIN + sessions, positions)


def test_placebo_permuted_event_sessions_lose_a_planted_relationship():
    event_offsets, stock, benchmark = planted_company()
    planted = run(events_on(event_offsets), stock, benchmark).positive
    assert planted.state is EvidenceState.DETECTED
    assert planted.primary.mean == pytest.approx(0.03, abs=0.01)

    # Same number of events, same prices, sessions drawn without reference to the planted ones.
    generator = np.random.default_rng(11)
    detected = 0
    for _ in range(20):
        placebo_offsets = sorted(
            generator.choice(400, size=len(event_offsets), replace=False).tolist()
        )
        result = run(events_on(placebo_offsets), stock, benchmark).positive
        assert result.state is not EvidenceState.NOT_ENOUGH_HISTORY
        detected += result.state is EvidenceState.DETECTED
    assert detected <= 2
