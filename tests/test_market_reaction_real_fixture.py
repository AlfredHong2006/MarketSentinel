"""Offline regression of the `mr-v1` engine on the MR-001 sanitized real-data fixture.

The fixture is a frozen research snapshot of inputs and carries no expected outputs, so every
number pinned here is structural (timing, deduplication, qualification, exclusivity, resolution).
No return outcome is asserted: this file must never become a place where results are tuned.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from marketsentinel.market_reaction import (
    EventStatus,
    EvidenceState,
    ListingExchange,
    PathCohort,
    PriceObservation,
    ReactionArticle,
    RegimeThresholds,
    TimingClass,
    analyze_market_reaction,
    select_thresholds,
)
from marketsentinel.market_reaction.signal import split_publication_stamp

FIXTURE = Path(__file__).parent / "fixtures" / "market_reaction" / "nvda_pfe_real_sample.json"
# Both at the floor, used as explicit provisional values. The frozen pair belongs to MR-003.
PROVISIONAL_THRESHOLDS = RegimeThresholds(negative=0.20, positive=0.20)
_LISTING_MARKETS = {"US": "S&P 500", "London": "FTSE 100"}


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def articles(payload) -> list[ReactionArticle]:
    converted = []
    for item in payload["articles"]:
        published_at, published_date = split_publication_stamp(
            datetime.fromisoformat(item["published_at"])
        )
        converted.append(
            ReactionArticle(
                article_id=item["article_id"],
                ticker=item["ticker"],
                title=item["title"],
                source=item["source"],
                published_at=published_at,
                published_date=published_date,
                p_positive=item["p_positive"],
                p_negative=item["p_negative"],
                p_neutral=item["p_neutral"],
                is_demo=item["is_demo"],
            )
        )
    return converted


def prices(payload, symbol: str) -> list[PriceObservation]:
    """The fixture predates the SPY/CUKX.L benchmark decision and carries index series only.

    They are used here purely as arithmetic regression input for the return alignment; this file
    asserts no return outcome, so the price-index/total-return difference cannot affect it.
    """

    return [
        PriceObservation(date=date.fromisoformat(item["date"]), adjusted_close=item["adj_close"])
        for item in payload["prices"][symbol]
    ]


def analyze(payload, articles, ticker: str):
    return analyze_market_reaction(
        ticker=ticker,
        listing_market=_LISTING_MARKETS[payload["_meta"]["listing"][ticker]],
        articles=articles,
        stock_prices=prices(payload, ticker),
        benchmark_prices=prices(payload, "^GSPC"),
        thresholds=PROVISIONAL_THRESHOLDS,
    )


def test_engine_timestamp_rule_matches_the_fixture_labels(payload, articles):
    for item, converted in zip(payload["articles"], articles, strict=True):
        quality = "full" if converted.published_at is not None else "date_only"
        assert quality == item["timestamp_quality"], item["article_id"]


def test_date_only_articles_never_land_on_their_own_stamped_date(payload, articles):
    dated = {item.article_id: item.published_date for item in articles if item.published_date}
    for ticker in ("NVDA", "PFE"):
        result = analyze(payload, articles, ticker)
        for signal in result.session_signals:
            for article_id in signal.article_ids:
                if article_id in dated:
                    assert signal.session > dated[article_id]


@pytest.mark.parametrize(
    ("ticker", "sessions", "eligible", "full", "date_only", "title_duplicates", "lagged"),
    [("NVDA", 40, 197, 111, 92, 6, 29), ("PFE", 37, 173, 77, 103, 7, 27)],
)
def test_signal_construction_on_real_articles(
    payload, articles, ticker, sessions, eligible, full, date_only, title_duplicates, lagged
):
    result = analyze(payload, articles, ticker)
    quality = result.data_quality
    assert result.exchange is ListingExchange.US
    assert result.signal_session_count == sessions
    assert quality.input_article_count == 383
    assert quality.eligible_article_count == eligible
    assert (quality.full_timestamp_count, quality.date_only_timestamp_count) == (full, date_only)
    assert quality.title_duplicates_removed == title_duplicates
    # Most real sessions are lagged: the corpus is dominated by date-only backfill stamps.
    assert quality.lagged_signal_session_count == lagged
    assert quality.ineligible_unusable_timestamp == 0
    assert quality.ineligible_invalid_sentiment == 0
    # Real yfinance series line up with the XNYS calendar exactly: no gaps, nothing off-calendar.
    assert (quality.stock_price_gap_sessions, quality.benchmark_price_gap_sessions) == (0, 0)
    assert quality.off_calendar_price_dates == 0
    assert quality.reasons == ()
    # Every article is accounted for exactly once across the sessions that used it.
    used = [article_id for signal in result.session_signals for article_id in signal.article_ids]
    assert len(used) == len(set(used)) == eligible


@pytest.mark.parametrize(
    ("ticker", "positive", "negative"),
    [
        ("NVDA", (11, 6, 1, 4), (6, 4, 1, 1)),
        ("PFE", (11, 5, 1, 5), (3, 2, 0, 1)),
    ],
)
def test_event_qualification_exclusivity_and_resolution(
    payload, articles, ticker, positive, negative
):
    result = analyze(payload, articles, ticker)
    for regime, expected in ((result.positive, positive), (result.negative, negative)):
        assert (
            regime.qualifying_event_count,
            regime.resolved_event_count,
            regime.pending_event_count,
            regime.suppressed_event_count,
        ) == expected
        assert regime.missing_price_event_count == 0
        # The company fails the history rule, so no regime may offer a verdict at all.
        assert regime.state is EvidenceState.NOT_ENOUGH_HISTORY
        kept = [event.session for event in regime.events if event.suppressed_by is None]
        assert kept == sorted(kept)
        threshold = (
            PROVISIONAL_THRESHOLDS.positive
            if regime is result.positive
            else PROVISIONAL_THRESHOLDS.negative
        )
        assert regime.threshold == threshold
        for event in regime.events:
            assert event.distinct_source_count >= 3
            assert abs(event.signal) >= threshold
            assert event.article_ids
            if event.status is EventStatus.RESOLVED:
                assert all(value is not None for value in event.market_adjusted_returns)


@pytest.mark.parametrize(
    ("ticker", "positive_day_zero", "negative_day_zero"),
    [("NVDA", 2, 2), ("PFE", 1, 0)],
)
def test_day_zero_cohort_is_smaller_than_the_forward_cohort_on_real_data(
    payload, articles, ticker, positive_day_zero, negative_day_zero
):
    result = analyze(payload, articles, ticker)
    for regime, expected in (
        (result.positive, positive_day_zero),
        (result.negative, negative_day_zero),
    ):
        exact = [
            event
            for event in regime.events
            if event.status is EventStatus.RESOLVED and event.timing_class is TimingClass.EXACT
        ]
        assert regime.day_zero_event_count == len(exact) == expected
        # Real lagged events really are dropped from day 0 and kept for the forward horizons.
        assert regime.day_zero_event_count < regime.resolved_event_count
        forward = regime.point_at(5)
        assert forward.cohort is PathCohort.ALL_RESOLVED
        assert forward.statistics.n == regime.resolved_event_count
        day_zero = regime.point_at(0)
        if expected:
            assert day_zero.cohort is PathCohort.EXACT_TIMING
            assert day_zero.statistics.n == expected
        else:
            assert day_zero is None


def test_lagged_events_carry_their_date_only_share(payload, articles):
    result = analyze(payload, articles, "NVDA")
    for event in result.positive.events:
        assert (event.date_only_share > 0) == (event.timing_class is TimingClass.LAGGED)
        assert 0.0 <= event.date_only_share <= 1.0


def test_prices_end_five_sessions_after_the_last_article_so_late_events_stay_pending(
    payload, articles
):
    result = analyze(payload, articles, "NVDA")
    (event,) = [item for item in result.positive.events if item.status is EventStatus.PENDING]
    assert event.session == date(2026, 9, 11)
    # +5 exists but +10 does not: the complete-path rule keeps it out of resolved statistics.
    assert event.market_adjusted_returns[5] is not None
    assert event.market_adjusted_returns[10] is None
    assert event.primary_market_adjusted_return is None


def test_holidays_and_weekends_never_carry_a_signal(payload, articles):
    result = analyze(payload, articles, "NVDA")
    sessions = {signal.session for signal in result.session_signals}
    assert date(2025, 11, 27) not in sessions  # Thanksgiving
    assert date(2026, 9, 7) not in sessions  # Labor Day
    assert all(session.weekday() < 5 for session in sessions)


def test_history_density_not_span_is_what_the_real_corpus_fails(payload, articles):
    for ticker in ("NVDA", "PFE"):
        history = analyze(payload, articles, ticker).history
        # Two disjoint sampling windows a year apart: a wide span over a sparse corpus.
        assert history.span_sessions >= 126
        assert history.meets_span
        assert history.signal_density < 0.50
        assert not history.meets_density
        assert not history.meets_qualified_count
        assert not history.sufficient
        assert analyze(payload, articles, ticker).state is EvidenceState.NOT_ENOUGH_HISTORY


def test_pooled_threshold_selection_runs_on_real_marginals_without_outcomes(payload, articles):
    signals = [
        signal
        for ticker in ("NVDA", "PFE")
        for signal in analyze(payload, articles, ticker).session_signals
    ]
    selection = select_thresholds(signals)
    assert selection.pooled_session_count == 77
    # Only sessions that could qualify as events shape the tails.
    assert selection.population_count == 53
    assert selection.thresholds.positive == pytest.approx(0.44)
    assert selection.thresholds.negative == pytest.approx(0.22)
    assert not (selection.positive_floor_applied or selection.negative_floor_applied)
    # The real marginal is positively skewed: a single symmetric threshold would have put very
    # different shares in the two tails, while cutting each tail separately balances them.
    assert selection.thresholds.positive > selection.thresholds.negative
    assert selection.positive_tail_share == pytest.approx(0.15, abs=0.02)
    assert selection.negative_tail_share == pytest.approx(0.15, abs=0.02)


def test_result_is_reproducible(payload, articles):
    assert analyze(payload, articles, "PFE") == analyze(payload, list(reversed(articles)), "PFE")
