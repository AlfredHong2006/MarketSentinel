"""The coverage ledger: explicit per-article states, no duplicate paid analysis, retryable
failures, per-provider watermarks, and a zero-spend activation.

Fully offline: scripted providers, a static sentiment backend, and throwaway databases.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_article, make_constituent, make_price_history
from fastapi.testclient import TestClient

from marketsentinel.analysis_ledger import (
    LedgeredArticleAnalysisRunner,
    deterministic_skip_reason,
    failure_transition,
    processing_order,
)
from marketsentinel.api.app import Services, build_services, create_app
from marketsentinel.config import Settings
from marketsentinel.constituents import CacheOnlyConstituentResolver
from marketsentinel.coverage_cycle import (
    CoverageCycleService,
    RecentProviderSource,
    _fetch_windows,
    _merge_fetch_results,
    fetch_outcome,
    fetch_window,
)
from marketsentinel.domain import (
    Article,
    ClaimAssessments,
    EventDirection,
    EventExtraction,
    EventType,
    IngestionFunnel,
    NewsFetchResult,
    RelatedCompanyProposals,
    SourceHealth,
    TimeHorizon,
    UniverseResult,
)
from marketsentinel.errors import (
    ArticleAnalysisProviderError,
    ArticleAnalysisSemanticValidationError,
    ArticleAnalysisStructuralValidationError,
    CoverageNotActiveError,
)
from marketsentinel.event_analysis import (
    ArticleEventAnalysisService,
    UnavailableArticleAnalysisProvider,
)
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts.run_coverage_cycle import build_coverage_service, build_parser

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class FakeConstituents:
    def resolve(self, symbol: str):
        assert symbol == "ACME"
        return make_constituent()

    def load(self) -> UniverseResult:
        return UniverseResult(
            constituents=[make_constituent()],
            source="test",
            is_fallback=False,
            fetched_at=T0,
        )


class ScriptedProvider:
    """Records calls and token usage like the real provider; can fail on demand."""

    model_version = "ledger-test-model"

    def __init__(self, failures: Sequence[Exception] = (), always: Exception | None = None):
        self.failures = list(failures)
        self.always = always
        self.stage_a_calls = 0
        self.last_usage: dict[str, tuple[int | None, int | None]] = {}

    def extract_event(self, request) -> EventExtraction:
        del request
        self.stage_a_calls += 1
        if self.always is not None:
            raise self.always
        if self.failures:
            raise self.failures.pop(0)
        self.last_usage["stage_a"] = (1000, 200)
        return EventExtraction(
            event_type=EventType.PARTNERSHIP,
            summary="Acme signed a multi-year supply agreement.",
            direction=EventDirection.POSITIVE,
            magnitude=0.5,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=0.8,
            important_claims=["Acme signed a multi-year supply agreement."],
            positive_channels=["Adds contracted supply revenue"],
        )

    def assess_claims(self, request) -> ClaimAssessments:
        del request
        self.last_usage["stage_b"] = (300, 50)
        return ClaimAssessments()

    def select_related_companies(self, request) -> RelatedCompanyProposals:
        del request
        self.last_usage["stage_c"] = (100, 20)
        return RelatedCompanyProposals()


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class ScriptedSource:
    name: str
    articles: list[Article] = field(default_factory=list)
    status: str = "healthy"
    provider_failures: int = 0
    request_limited: int = 0
    max_lookback: timedelta = timedelta(days=30)
    max_articles: int = 100
    calls: list[tuple[datetime, datetime]] = field(default_factory=list)

    def fetch(self, constituent, since, until) -> NewsFetchResult:
        del constituent
        self.calls.append((since, until))
        return NewsFetchResult(
            articles=[] if self.status == "unavailable" else list(self.articles),
            health=SourceHealth(provider=self.name, status=self.status),
            funnel=IngestionFunnel(
                provider_failures=self.provider_failures, request_limited=self.request_limited
            ),
        )


@dataclass
class Harness:
    repository: SQLiteRepository
    provider: object
    service: ArticleEventAnalysisService
    runner: LedgeredArticleAnalysisRunner
    cycle: CoverageCycleService
    clock: Clock

    @property
    def contract(self) -> str:
        return self.runner.compatibility.contract_key

    def job(self, article: Article):
        return self.repository.get_analysis_job(article.fingerprint, self.contract)

    def run(self, max_new: int = 10, ingest: bool = True):
        return self.cycle.run("ACME", now=self.clock.now, max_new_analyses=max_new, ingest=ingest)


def build(
    tmp_path,
    provider=None,
    sources: Sequence[ScriptedSource] = (),
    *,
    allow_terminal_retry: bool = False,
) -> Harness:
    repository = SQLiteRepository(tmp_path / "ledger.db")
    repository.initialize()
    provider = provider or ScriptedProvider()
    clock = Clock()
    service = ArticleEventAnalysisService(
        repository=repository, provider=provider, constituents=FakeConstituents()
    )
    runner = LedgeredArticleAnalysisRunner(
        repository,
        service,
        service.compatibility,
        allow_terminal_retry=allow_terminal_retry,
        clock=clock,
    )
    cycle = CoverageCycleService(
        constituents=FakeConstituents(),
        sources=sources,
        sentiment=StaticSentimentAnalyzer(),
        repository=repository,
        runner=runner,
    )
    return Harness(repository, provider, service, runner, cycle, clock)


def article(index: int, published_at: datetime, **updates) -> Article:
    item = make_article(
        title=f"Acme Corporation signs supply agreement number {index}",
        published_at=published_at,
        url=f"https://wire{index}.example/acme-{index}",
        source=f"Wire {index}",
    )
    return item.model_copy(update=updates) if updates else item


# --------------------------------------------------------------------------------------------
# Migration and repository primitives
# --------------------------------------------------------------------------------------------


def test_initialize_migrates_a_version_4_database_and_keeps_existing_rows(writable_tmp_path):
    repository = SQLiteRepository(writable_tmp_path / "old.db")
    repository.initialize()
    stored = article(1, T0)
    repository.upsert_articles([stored])
    with sqlite3.connect(repository.path) as connection:
        for table in ("article_analysis_jobs", "ingestion_watermarks", "company_coverage"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version = 4")

    repository.initialize()
    repository.initialize()

    with sqlite3.connect(repository.path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"article_analysis_jobs", "ingestion_watermarks", "company_coverage"} <= tables
    assert version == 5
    assert repository.get_article(stored.fingerprint) == stored


def test_claim_is_exclusive_and_reclaiming_an_expired_lease_counts_the_abandoned_attempt(
    writable_tmp_path,
):
    h = build(writable_tmp_path)
    h.repository.activate_company_coverage("ACME", started_at=T0, live_window_days=30)
    item = article(1, T0 - timedelta(hours=1))
    h.repository.upsert_articles([item])
    h.run(max_new=0, ingest=False)

    lease = T0 + timedelta(minutes=10)
    first = h.repository.claim_analysis_job(
        item.fingerprint, h.contract, owner="a", now=T0, lease_expires_at=lease
    )
    second = h.repository.claim_analysis_job(
        item.fingerprint, h.contract, owner="b", now=T0, lease_expires_at=lease
    )
    assert first is not None and first.attempts == 0
    assert second is None

    later = lease + timedelta(seconds=1)
    reclaimed = h.repository.claim_analysis_job(
        item.fingerprint,
        h.contract,
        owner="b",
        now=later,
        lease_expires_at=later + timedelta(minutes=10),
    )
    assert reclaimed is not None
    assert reclaimed.lease_owner == "b"
    assert reclaimed.attempts == 1
    assert reclaimed.last_failure_category == "lease_expired"
    # The stale owner can no longer move the job.
    assert not h.repository.finish_analysis_job(
        item.fingerprint,
        h.contract,
        owner="a",
        now=later,
        state="analyzed",
        reason="generated",
        paid_attempt=False,
        input_tokens=0,
        output_tokens=0,
    )


def test_watermarks_never_move_backwards_and_a_failure_keeps_the_last_one(writable_tmp_path):
    repository = SQLiteRepository(writable_tmp_path / "wm.db")
    repository.initialize()

    def record(status: str, through: datetime | None, at: datetime) -> None:
        repository.record_ingestion_attempt(
            ticker="ACME",
            provider="gdelt",
            window_start=at - timedelta(days=1),
            attempted_at=at,
            status=status,
            message=None,
            articles=0,
            request_limited=0,
            ingested_through=through,
        )

    record("ok", T0, T0)
    record("failed", None, T0 + timedelta(hours=1))
    record("failed", None, T0 + timedelta(hours=2))
    watermark = repository.get_ingestion_watermark("ACME", "gdelt")
    assert watermark.ingested_through == T0
    assert watermark.consecutive_failures == 2

    record("ok", T0 - timedelta(days=3), T0 + timedelta(hours=3))
    watermark = repository.get_ingestion_watermark("ACME", "gdelt")
    assert watermark.ingested_through == T0
    assert watermark.consecutive_failures == 0


def test_partial_fetches_count_as_consecutive_problems_until_ok_or_empty_resets(
    writable_tmp_path,
):
    repository = SQLiteRepository(writable_tmp_path / "wm-partial.db")
    repository.initialize()

    def record(status: str, through: datetime | None, at: datetime) -> None:
        repository.record_ingestion_attempt(
            ticker="ACME",
            provider="gdelt",
            window_start=at - timedelta(days=1),
            attempted_at=at,
            status=status,
            message=None,
            articles=1,
            request_limited=3 if status == "partial" else 0,
            ingested_through=through,
        )

    def watermark():
        return repository.get_ingestion_watermark("ACME", "gdelt")

    record("partial", None, T0)
    assert (watermark().consecutive_failures, watermark().ingested_through) == (1, None)
    assert watermark().last_success_at is None

    record("ok", T0, T0 + timedelta(hours=1))
    record("partial", None, T0 + timedelta(hours=2))
    record("failed", None, T0 + timedelta(hours=3))
    record("partial", None, T0 + timedelta(hours=4))
    assert watermark().consecutive_failures == 3
    assert watermark().last_status == "partial"
    assert watermark().last_request_limited == 3
    assert watermark().ingested_through == T0
    assert watermark().last_success_at == T0 + timedelta(hours=1)

    record("empty", T0 + timedelta(hours=5), T0 + timedelta(hours=5))
    assert watermark().consecutive_failures == 0
    record("partial", None, T0 + timedelta(hours=6))
    record("ok", T0 + timedelta(hours=7), T0 + timedelta(hours=7))
    assert watermark().consecutive_failures == 0
    assert watermark().ingested_through == T0 + timedelta(hours=7)


# --------------------------------------------------------------------------------------------
# Pure policy helpers
# --------------------------------------------------------------------------------------------


def test_fetch_window_uses_the_lookback_floor_overlap_and_flags_unreachable_gaps():
    lookback = timedelta(days=7)
    overlap = timedelta(hours=48)
    assert fetch_window(None, T0, overlap=overlap, max_lookback=lookback) == (T0 - lookback, False)
    assert fetch_window(T0 - timedelta(hours=6), T0, overlap=overlap, max_lookback=lookback) == (
        T0 - timedelta(hours=54),
        False,
    )
    assert fetch_window(T0 - timedelta(days=10), T0, overlap=overlap, max_lookback=lookback) == (
        T0 - lookback,
        True,
    )


def test_fetch_outcome_only_completes_fetches_without_provider_failure():
    def result(status="healthy", failures=0, limited=0, articles=()):
        return NewsFetchResult(
            articles=list(articles),
            health=SourceHealth(provider="p", status=status),
            funnel=IngestionFunnel(provider_failures=failures, request_limited=limited),
        )

    assert fetch_outcome(result(articles=[article(1, T0)]))[:2] == ("ok", True)
    assert fetch_outcome(result(status="degraded"))[:2] == ("empty", True)
    assert fetch_outcome(result(status="unavailable"))[:2] == ("failed", False)
    assert fetch_outcome(result(status="degraded", failures=1))[:2] == ("failed", False)
    status, completed, message = fetch_outcome(result(limited=4, articles=[article(1, T0)]))
    assert (status, completed) == ("partial", False)
    assert "4 article(s)" in message
    assert "not advanced" in message


def test_fetch_windows_splits_a_span_into_contiguous_non_overlapping_chunks():
    since, until = T0 - timedelta(days=2, hours=12), T0
    windows = _fetch_windows(since, until, timedelta(days=1))

    assert len(windows) == 3
    assert windows[0][0] == since
    assert windows[-1][1] == until
    # Each window is at most one step, and consecutive windows share a boundary with no gap.
    assert all(end - start <= timedelta(days=1) for start, end in windows)
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:], strict=False))


def test_fetch_windows_handles_a_span_no_longer_than_one_step_and_a_zero_length_span():
    assert _fetch_windows(T0 - timedelta(hours=6), T0, timedelta(days=1)) == [
        (T0 - timedelta(hours=6), T0)
    ]
    assert _fetch_windows(T0, T0, timedelta(days=1)) == []


def _fake_result(*, status="healthy", articles=(), limited=0, failures=0, message=None, latency=0):
    return NewsFetchResult(
        articles=list(articles),
        health=SourceHealth(provider="p", status=status, message=message, latency_ms=latency),
        funnel=IngestionFunnel(request_limited=limited, provider_failures=failures),
    )


def test_merge_fetch_results_sums_funnels_and_takes_the_worst_health_status():
    merged = _merge_fetch_results(
        [
            _fake_result(articles=[article(1, T0)], latency=100),
            _fake_result(status="degraded", limited=2, message="capped", latency=50),
        ]
    )

    assert len(merged.articles) == 1
    assert merged.funnel.request_limited == 2
    assert merged.health.status == "degraded"
    assert merged.health.latency_ms == 150
    assert merged.health.message == "capped"

    # A provider failure in any one window makes the combined result unavailable, exactly as a
    # single flat fetch failing would -- fetch_outcome must still see this as "failed".
    merged = _merge_fetch_results(
        [_fake_result(), _fake_result(status="unavailable", failures=1, message="HTTP 429")]
    )
    assert fetch_outcome(merged)[:2] == ("failed", False)


def test_merge_fetch_results_of_no_windows_is_a_harmless_empty_healthy_result():
    merged = _merge_fetch_results([])
    assert merged.articles == []
    assert merged.health.status == "healthy"
    assert fetch_outcome(merged)[:2] == ("empty", True)


@dataclass
class BoundedFakeProvider:
    """A fake recent-news provider whose own result cap applies per request, like a real one.

    A request for ``[since, until]`` returns at most ``max_articles`` of the pool published in
    that exact span (newest first) and reports the rest as ``request_limited`` -- exactly the
    per-request capping behaviour ``RecentProviderSource`` is windowing around.
    """

    pool: list[Article] = field(default_factory=list)
    calls: list[tuple[datetime, datetime | None]] = field(default_factory=list)

    def fetch(self, constituent, since, max_articles, *, until=None) -> NewsFetchResult:
        self.calls.append((since, until))
        upper = until if until is not None else since
        window = [item for item in self.pool if since <= item.published_at <= upper]
        selected = sorted(window, key=lambda item: item.published_at, reverse=True)[:max_articles]
        return NewsFetchResult(
            articles=selected,
            health=SourceHealth(provider="fake", status="healthy" if selected else "degraded"),
            funnel=IngestionFunnel(request_limited=len(window) - len(selected)),
        )


def test_windowing_a_dense_recent_source_converges_instead_of_capping_the_whole_span():
    now = T0
    since = now - timedelta(days=7)
    # ~13 articles/day for a week: comfortably under a 50/day cap, but 90 total is far over a
    # single 50-article flat cap for the whole span -- the real NVDA/PFE shape this fix targets.
    # Offset by a few minutes so no timestamp lands exactly on a day boundary shared by two
    # windows (an inclusive-inclusive boundary instant would otherwise count in both).
    pool = [article(index, since + timedelta(hours=index * 1.8, minutes=3)) for index in range(90)]
    provider = BoundedFakeProvider(pool=pool)
    source = RecentProviderSource(
        name="google_news_rss", provider=provider, max_lookback=timedelta(days=7), max_articles=50
    )

    result = source.fetch(make_constituent(), since, now)

    assert len(result.articles) == 90  # nothing the cap would have cut off is lost
    assert result.funnel.request_limited == 0
    assert fetch_outcome(result)[:2] == ("ok", True)  # converges: the watermark may now advance
    assert len(provider.calls) == 7  # one request per day-sized window, not one flat request


def test_windowing_converges_the_exact_reported_single_day_over_the_old_budget():
    """The real NVDA shape: one bounded day alone carried more candidates (93) than the
    interactive-refresh budget (``max_articles=50``, matching ``news_max_articles``'s default) but
    fewer than Google's own per-request ceiling -- "50 kept + 43 unseen" in the real run. Each
    window's own request must reach that real ceiling instead of re-imposing the smaller budget a
    second time, so this day now converges instead of retrying the same window forever.
    """

    now = T0
    since = now - timedelta(days=7)
    dense_day = now - timedelta(days=3)  # exactly one _fetch_windows day boundary
    # Offset by a minute so no timestamp lands exactly on the boundary shared with the previous
    # window (an inclusive-inclusive boundary instant would otherwise count in both).
    pool = [article(index, dense_day + timedelta(minutes=1 + index * 15)) for index in range(93)]
    provider = BoundedFakeProvider(pool=pool)
    source = RecentProviderSource(
        name="google_news_rss", provider=provider, max_lookback=timedelta(days=7), max_articles=50
    )

    result = source.fetch(make_constituent(), since, now)

    assert len(result.articles) == 93  # none of the "43 unseen" articles are lost this time
    assert result.funnel.request_limited == 0
    assert fetch_outcome(result)[:2] == ("ok", True)  # converges: the watermark may now advance
    dense_call = next(call for call in provider.calls if call[0] == dense_day)
    assert dense_call[1] == dense_day + timedelta(days=1)


def test_windowing_still_reports_partial_when_a_single_window_exceeds_googles_own_cap():
    now = T0
    since = now - timedelta(days=2)
    # 110 articles inside the final day alone: denser than even Google's own per-request ceiling
    # (100), so no amount of windowing can resolve it -- the residual hard limit this fix cannot
    # remove. Still lossless: the watermark must not advance past the 10 unseen articles.
    pool = [article(index, now - timedelta(minutes=index)) for index in range(110)]
    provider = BoundedFakeProvider(pool=pool)
    source = RecentProviderSource(
        name="google_news_rss", provider=provider, max_lookback=timedelta(days=7), max_articles=50
    )

    result = source.fetch(make_constituent(), since, now)

    assert result.funnel.request_limited == 10
    assert fetch_outcome(result)[:2] == ("partial", False)


def test_failure_transition_backs_off_transient_errors_and_bounds_validation_retries():
    assert failure_transition("timeout", 1, T0) == ("retry_wait", T0 + timedelta(minutes=15))
    assert failure_transition("http_error", 2, T0) == ("retry_wait", T0 + timedelta(hours=1))
    assert failure_transition("transport_error", 3, T0) == ("retry_wait", T0 + timedelta(hours=4))
    assert failure_transition("timeout", 4, T0) == ("failed", None)
    assert failure_transition("semantic_validation", 1, T0)[0] == "retry_wait"
    assert failure_transition("semantic_validation", 2, T0) == ("failed", None)
    assert failure_transition("something_new", 1, T0) == ("failed", None)


def test_only_per_article_irrelevance_rules_produce_a_skip_reason():
    subject = make_constituent()
    assert deterministic_skip_reason(article(1, T0), subject) is None
    assert deterministic_skip_reason(article(1, T0, relevance_score=0.2), subject) == (
        "low_relevance"
    )
    assert deterministic_skip_reason(article(1, T0, is_demo=True), subject) == "demo"
    prediction = make_article(title="Acme Corporation stock price prediction for 2027")
    assert deterministic_skip_reason(prediction, subject) == "price_prediction"


def test_processing_order_defers_capped_articles_but_never_drops_them():
    same_publisher = [
        make_article(
            title=f"Acme Corporation signs supply agreement in region {index}",
            published_at=T0 - timedelta(hours=index),
            url=f"https://wire.example/{index}",
            source="One Wire",
        )
        for index in range(6)
    ]

    ordered = processing_order(same_publisher, T0, make_constituent())

    assert sorted(item.fingerprint for item in ordered) == sorted(
        item.fingerprint for item in same_publisher
    )
    assert len(ordered) == 6


# --------------------------------------------------------------------------------------------
# Activation
# --------------------------------------------------------------------------------------------


def test_activation_spends_nothing_and_gives_every_stored_article_one_state(writable_tmp_path):
    source = ScriptedSource("gdelt")
    h = build(writable_tmp_path, sources=[source])
    old_analysed = article(1, T0 - timedelta(days=90))
    old_unanalysed = article(2, T0 - timedelta(days=60))
    recent = article(3, T0 - timedelta(days=2))
    recent_irrelevant = article(4, T0 - timedelta(days=1), relevance_score=0.1)
    h.repository.upsert_articles([old_analysed, old_unanalysed, recent, recent_irrelevant])
    assert h.service.analyze_article(old_analysed.fingerprint).status == "generated"
    calls_before = h.provider.stage_a_calls

    report = h.cycle.activate("ACME", now=T0)

    assert h.provider.stage_a_calls == calls_before
    assert source.calls == []
    assert (h.job(old_analysed).state, h.job(old_analysed).reason) == ("analyzed", "preexisting")
    assert (h.job(old_unanalysed).state, h.job(old_unanalysed).reason) == (
        "baseline",
        "pre_ledger_history",
    )
    assert (h.job(recent).state, h.job(recent).reason) == ("pending", "eligible")
    assert (h.job(recent_irrelevant).state, h.job(recent_irrelevant).reason) == (
        "skipped",
        "low_relevance",
    )
    assert report.summary.articles_without_job == 0

    again = h.cycle.activate("ACME", now=T0 + timedelta(days=5))
    assert again.reconcile.created == {}
    assert again.coverage.ledger_started_at == T0


def test_a_cycle_refuses_a_ticker_that_was_never_activated(writable_tmp_path):
    h = build(writable_tmp_path)
    with pytest.raises(CoverageNotActiveError):
        h.run()


# --------------------------------------------------------------------------------------------
# The cycle: eligibility now, exactly-once spend, budget deferral
# --------------------------------------------------------------------------------------------


def test_a_new_article_is_analysed_in_the_same_cycle_and_never_paid_for_twice(writable_tmp_path):
    fresh = article(1, T0 + timedelta(minutes=30))
    source = ScriptedSource("gdelt", articles=[fresh])
    h = build(writable_tmp_path, sources=[source])
    h.cycle.activate("ACME", now=T0)
    h.clock.now = T0 + timedelta(hours=1)

    first = h.run()

    assert first.ingest.new_articles == 1
    assert first.analysis.generated == 1
    job = h.job(fresh)
    assert (job.state, job.reason, job.attempts) == ("analyzed", "generated", 1)
    assert (job.input_tokens, job.output_tokens) == (1300, 250)
    assert h.provider.stage_a_calls == 1

    h.clock.now = T0 + timedelta(hours=2)
    second = h.run()

    assert second.ingest.new_articles == 0
    assert second.analysis.claimable == 0
    assert h.provider.stage_a_calls == 1
    assert second.summary.articles_without_job == 0


def test_budget_limited_work_stays_pending_and_is_reached_by_later_cycles(writable_tmp_path):
    items = [article(index, T0 - timedelta(hours=index)) for index in range(1, 6)]
    h = build(writable_tmp_path, sources=[ScriptedSource("gdelt", articles=items)])
    h.cycle.activate("ACME", now=T0 - timedelta(days=1))

    first = h.run(max_new=2)

    assert (first.analysis.generated, first.analysis.deferred) == (2, 3)
    assert first.analysis.stop_reason == "budget"
    assert first.summary.states == {"analyzed": 2, "pending": 3}

    h.run(max_new=2)
    third = h.run(max_new=2)

    assert third.summary.states == {"analyzed": 5}
    assert h.provider.stage_a_calls == 5


def test_evidence_drift_reuses_the_analysis_records_staleness_and_refresh_stays_explicit(
    writable_tmp_path,
):
    first_article = article(1, T0 - timedelta(days=3))
    follow_up = article(2, T0 - timedelta(days=2))
    source = ScriptedSource("gdelt", articles=[first_article])
    h = build(writable_tmp_path, sources=[source])
    h.cycle.activate("ACME", now=T0 - timedelta(days=4))
    h.run()
    original_fingerprint = h.job(first_article).analysis_evidence_fingerprint
    assert h.job(first_article).evidence_current is True

    source.articles = [first_article, follow_up]
    report = h.run()

    assert h.provider.stage_a_calls == 2  # the follow-up only; the first article is not re-paid
    assert report.evidence.changed == 1
    assert h.job(first_article).evidence_current is False
    assert h.job(first_article).analysis_evidence_fingerprint == original_fingerprint
    assert "refresh-evidence" in report.render()

    # The explicit refresh path is unchanged: it regenerates because the evidence changed.
    assert h.service.analyze_article(first_article.fingerprint).status == "generated"
    after_refresh = h.run(ingest=False)

    assert h.provider.stage_a_calls == 3
    assert after_refresh.evidence.changed == 0
    assert h.job(first_article).evidence_current is True
    assert h.job(first_article).analysis_evidence_fingerprint != original_fingerprint


def test_articles_stored_outside_the_cycle_are_reconciled_into_one_state(writable_tmp_path):
    h = build(writable_tmp_path)
    h.cycle.activate("ACME", now=T0)
    backfilled_history = article(1, T0 - timedelta(days=200))
    stored_by_refresh = article(2, T0 + timedelta(hours=1))
    h.repository.upsert_articles([backfilled_history, stored_by_refresh])
    h.clock.now = T0 + timedelta(hours=2)

    report = h.run(ingest=False)

    assert h.job(backfilled_history).state == "baseline"
    assert h.job(stored_by_refresh).state == "analyzed"
    assert report.summary.articles_without_job == 0


# --------------------------------------------------------------------------------------------
# Contract/version bumps
# --------------------------------------------------------------------------------------------


def test_a_contract_bump_requeues_live_window_work_and_leaves_baseline_and_history_alone(
    writable_tmp_path,
):
    """A relevant analysis-contract change (here, a prompt version) must, through the same DB:

    - leave every old-contract job row exactly as it was;
    - make live-window work claimable again under the new contract, and pay for it once;
    - never pay twice for a result already compatible with the new contract;
    - never requeue pre-horizon/baseline history.

    No network or real LLM call: the shared ``ScriptedProvider`` only ever returns canned data.
    """

    h = build(writable_tmp_path)
    h.cycle.activate("ACME", now=T0)

    baseline_old = article(1, T0 - timedelta(days=60))  # older than the live-window horizon
    analyzed_recent = article(2, T0 - timedelta(days=5))  # in the live window
    pending_recent = article(3, T0 - timedelta(days=2))  # in the live window
    h.repository.upsert_articles([baseline_old, analyzed_recent, pending_recent])
    h.cycle.activate("ACME", now=T0)  # reconcile the new articles under the original contract

    old_contract = h.contract
    assert h.job(baseline_old).state == "baseline"
    assert h.job(pending_recent).state == "pending"

    paid_before = h.provider.stage_a_calls
    outcome = h.runner.process(analyzed_recent.fingerprint, now=T0)
    assert outcome.response.status == "generated"
    assert h.provider.stage_a_calls == paid_before + 1
    old_analyzed_job = h.job(analyzed_recent)
    old_pending_job = h.job(pending_recent)
    assert old_analyzed_job.state == "analyzed"

    # A relevant analysis-contract change: same repository, a bumped prompt version.
    bumped_service = ArticleEventAnalysisService(
        repository=h.repository,
        provider=h.provider,
        constituents=FakeConstituents(),
        stage_a_prompt_version="event-extraction-bumped",
    )
    bumped_runner = LedgeredArticleAnalysisRunner(
        h.repository, bumped_service, bumped_service.compatibility, clock=h.clock
    )
    bumped_cycle = CoverageCycleService(
        constituents=FakeConstituents(),
        sources=(),
        sentiment=StaticSentimentAnalyzer(),
        repository=h.repository,
        runner=bumped_runner,
    )
    new_contract = bumped_runner.compatibility.contract_key
    assert new_contract != old_contract

    def new_job(item: Article):
        return h.repository.get_analysis_job(item.fingerprint, new_contract)

    reconciled = bumped_cycle.activate("ACME", now=T0)

    # Live-window work is claimable under the new contract; baseline is not requeued.
    assert reconciled.reconcile.created == {"baseline": 1, "pending": 2}
    assert new_job(baseline_old).state == "baseline"
    assert new_job(analyzed_recent).state == "pending"
    assert new_job(pending_recent).state == "pending"
    assert reconciled.coverage.ledger_started_at == T0  # the horizon does not move with a contract

    # The old contract's job rows are untouched history.
    assert h.job(analyzed_recent) == old_analyzed_job
    assert h.job(pending_recent) == old_pending_job

    # Real, once-only spend to satisfy the new contract's live-window work.
    paid_before = h.provider.stage_a_calls
    result = bumped_cycle.run("ACME", now=T0, max_new_analyses=5, ingest=False)
    assert result.analysis.generated == 2
    assert h.provider.stage_a_calls == paid_before + 2
    assert new_job(analyzed_recent).state == "analyzed"
    assert new_job(pending_recent).state == "analyzed"
    assert new_job(baseline_old).state == "baseline"  # never touched, never paid for

    # A second pass under the same new contract must not repay for now-compatible results.
    paid_before = h.provider.stage_a_calls
    again = bumped_cycle.run("ACME", now=T0, max_new_analyses=5, ingest=False)
    assert again.reconcile.created == {}
    assert again.analysis.claimable == 0
    assert h.provider.stage_a_calls == paid_before


# --------------------------------------------------------------------------------------------
# Failures, retries, crash recovery
# --------------------------------------------------------------------------------------------


def _single_pending(h: Harness) -> Article:
    item = article(1, T0 - timedelta(hours=1))
    h.cycle.activate("ACME", now=T0 - timedelta(days=1))
    h.repository.upsert_articles([item])
    return item


def test_transient_failures_retry_with_backoff_then_fail_permanently(writable_tmp_path):
    h = build(writable_tmp_path, ScriptedProvider(always=ArticleAnalysisProviderError("timeout")))
    item = _single_pending(h)

    h.run()
    job = h.job(item)
    assert (job.state, job.attempts, job.failure_count) == ("retry_wait", 1, 1)
    assert job.last_failure_category == "timeout"
    assert job.next_attempt_at == T0 + timedelta(minutes=15)

    h.clock.now = T0 + timedelta(minutes=5)
    assert h.run().analysis.claimable == 0
    assert h.provider.stage_a_calls == 1

    for due in (
        timedelta(minutes=16),
        timedelta(hours=1, minutes=17),
        timedelta(hours=5, minutes=18),
    ):
        h.clock.now = T0 + due
        h.run()
    job = h.job(item)
    assert (job.state, job.attempts, job.reason) == ("failed", 4, "timeout")

    h.clock.now = T0 + timedelta(days=2)
    h.run()
    assert h.provider.stage_a_calls == 4


def test_a_validation_failure_is_retried_once_before_becoming_permanent(writable_tmp_path):
    h = build(
        writable_tmp_path,
        ScriptedProvider(always=ArticleAnalysisSemanticValidationError("bad evidence")),
    )
    item = _single_pending(h)

    h.run()
    h.clock.now = T0 + timedelta(minutes=16)
    h.run()

    job = h.job(item)
    assert (job.state, job.attempts, job.last_failure_category) == (
        "failed",
        2,
        "semantic_validation",
    )


def test_an_unconfigured_provider_spends_no_attempt_and_leaves_work_pending(writable_tmp_path):
    h = build(writable_tmp_path, UnavailableArticleAnalysisProvider())
    item = _single_pending(h)

    report = h.run()

    assert report.analysis.stop_reason == "provider_unavailable"
    job = h.job(item)
    assert (job.state, job.attempts, job.failure_count) == ("pending", 0, 0)


def test_the_circuit_breaker_stops_after_two_consecutive_paid_failures(writable_tmp_path):
    h = build(
        writable_tmp_path, ScriptedProvider(always=ArticleAnalysisProviderError("http_error"))
    )
    h.cycle.activate("ACME", now=T0 - timedelta(days=1))
    h.repository.upsert_articles([article(i, T0 - timedelta(hours=i)) for i in range(1, 4)])

    report = h.run()

    assert report.analysis.stop_reason == "circuit_breaker"
    assert report.analysis.paid_attempts == 2
    assert report.analysis.deferred == 1
    assert report.summary.states == {"retry_wait": 2, "pending": 1}


def test_a_crash_after_the_analysis_was_stored_is_recovered_without_paying_again(
    writable_tmp_path,
):
    h = build(writable_tmp_path)
    item = _single_pending(h)
    h.run(max_new=0, ingest=False)
    h.repository.claim_analysis_job(
        item.fingerprint,
        h.contract,
        owner="crashed-process",
        now=T0,
        lease_expires_at=T0 + timedelta(minutes=10),
    )
    assert h.service.analyze_article(item.fingerprint).status == "generated"  # then it "crashed"

    h.clock.now = T0 + timedelta(minutes=5)
    assert h.run(ingest=False).analysis.claimable == 0

    h.clock.now = T0 + timedelta(minutes=11)
    report = h.run(ingest=False)

    assert report.analysis.reused == 1
    assert h.provider.stage_a_calls == 1
    assert (h.job(item).state, h.job(item).reason) == ("analyzed", "preexisting")


def test_an_abandoned_lease_without_a_stored_analysis_is_reclaimed_and_counted(
    writable_tmp_path,
):
    h = build(writable_tmp_path)
    item = _single_pending(h)
    h.run(max_new=0, ingest=False)
    h.repository.claim_analysis_job(
        item.fingerprint,
        h.contract,
        owner="crashed-process",
        now=T0,
        lease_expires_at=T0 + timedelta(minutes=10),
    )
    h.clock.now = T0 + timedelta(minutes=11)

    h.run(ingest=False)

    job = h.job(item)
    assert (job.state, job.attempts, job.failure_count) == ("analyzed", 2, 1)


# --------------------------------------------------------------------------------------------
# Per-provider watermarks
# --------------------------------------------------------------------------------------------


def test_provider_watermarks_advance_independently_and_overlap_collects_late_arrivals(
    writable_tmp_path,
):
    gdelt = ScriptedSource("gdelt")
    rss = ScriptedSource("rss", status="unavailable", max_lookback=timedelta(days=7))
    h = build(writable_tmp_path, sources=[gdelt, rss])
    h.cycle.activate("ACME", now=T0 - timedelta(days=1))

    h.run()
    assert h.repository.get_ingestion_watermark("ACME", "gdelt").ingested_through == T0
    rss_watermark = h.repository.get_ingestion_watermark("ACME", "rss")
    assert rss_watermark.ingested_through is None
    assert rss_watermark.last_status == "failed"

    late = article(9, T0 - timedelta(hours=3))  # published before the gdelt watermark
    gdelt.articles = [late]
    h.clock.now = T0 + timedelta(hours=6)
    report = h.run()

    assert gdelt.calls[-1][0] == T0 - timedelta(hours=48)
    assert rss.calls[-1][0] == h.clock.now - timedelta(days=7)
    assert report.ingest.new_articles == 1
    assert h.job(late).state == "analyzed"
    assert h.repository.get_ingestion_watermark("ACME", "rss").consecutive_failures == 2


def test_a_provider_cap_is_recorded_as_partial_coverage(writable_tmp_path):
    capped = ScriptedSource("gdelt", articles=[article(1, T0)], request_limited=7)
    h = build(writable_tmp_path, sources=[capped])
    h.cycle.activate("ACME", now=T0 - timedelta(days=1))

    report = h.run(max_new=0)

    watermark = h.repository.get_ingestion_watermark("ACME", "gdelt")
    assert (watermark.last_status, watermark.last_request_limited) == ("partial", 7)
    assert watermark.ingested_through is None
    assert watermark.consecutive_failures == 1
    assert "cap reached" in report.render()
    # The articles the capped fetch did return are still stored and enter the ledger.
    assert report.ingest.new_articles == 1


def test_a_partial_fetch_keeps_the_watermark_and_the_next_cycle_retries_the_unresolved_window(
    writable_tmp_path,
):
    source = ScriptedSource("gdelt", articles=[article(1, T0 - timedelta(hours=1))])
    h = build(writable_tmp_path, sources=[source])
    h.cycle.activate("ACME", now=T0 - timedelta(days=1))
    h.run(max_new=0)
    assert h.repository.get_ingestion_watermark("ACME", "gdelt").ingested_through == T0

    # A dense window: the provider hits its cap and keeps only the newest article.
    source.articles = [article(2, T0 + timedelta(hours=5))]
    source.request_limited = 4
    h.clock.now = T0 + timedelta(hours=6)
    h.run(max_new=0)

    watermark = h.repository.get_ingestion_watermark("ACME", "gdelt")
    assert (watermark.ingested_through, watermark.last_status) == (T0, "partial")
    assert watermark.consecutive_failures == 1

    # The next cycle starts from the previous watermark again, so the older capped article is
    # reachable once the provider can return it.
    capped_out = article(3, T0 + timedelta(hours=2))
    source.articles = [capped_out, article(2, T0 + timedelta(hours=5))]
    source.request_limited = 0
    h.clock.now = T0 + timedelta(hours=12)
    report = h.run(max_new=0)

    assert source.calls[-1][0] == T0 - timedelta(hours=48)
    assert report.ingest.new_articles == 1
    assert h.job(capped_out).state == "pending"
    watermark = h.repository.get_ingestion_watermark("ACME", "gdelt")
    assert (watermark.ingested_through, watermark.last_status) == (h.clock.now, "ok")
    assert watermark.consecutive_failures == 0


# --------------------------------------------------------------------------------------------
# Failure categories and the private API spending paths
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "category"),
    [
        (ScriptedProvider(always=ArticleAnalysisProviderError("timeout")), "timeout"),
        (
            ScriptedProvider(always=ArticleAnalysisSemanticValidationError("x")),
            "semantic_validation",
        ),
        (
            ScriptedProvider(always=ArticleAnalysisStructuralValidationError("x")),
            "pydantic_validation",
        ),
        (ScriptedProvider(always=RuntimeError("boom")), "unexpected"),
        (UnavailableArticleAnalysisProvider(), "unavailable"),
    ],
)
def test_analysis_failures_carry_a_machine_readable_category(writable_tmp_path, provider, category):
    h = build(writable_tmp_path, provider)
    item = article(1, T0)
    h.repository.upsert_articles([item])

    assert h.service.analyze_article(item.fingerprint).failure_category == category


class StaticNews:
    def __init__(self, articles: Sequence[Article]) -> None:
        self.articles = list(articles)

    def fetch_result(self, constituent, since, max_articles):
        del constituent, since, max_articles
        health = SourceHealth(provider="static-rss", status="healthy")
        return NewsFetchResult(articles=list(self.articles), health=health), [health]


class StaticPrices:
    def fetch(self, constituent):
        del constituent
        return make_price_history()


def test_private_refresh_routes_through_the_ledger_and_does_not_repay_after_evidence_drift(
    writable_tmp_path,
):
    now = datetime.now(UTC)
    primary = article(1, now - timedelta(hours=5))
    h = build(writable_tmp_path)
    service = MarketAnalysisService(
        constituents=FakeConstituents(),
        news=StaticNews([primary]),
        historical_news=None,
        sentiment=StaticSentimentAnalyzer(),
        prices=StaticPrices(),
        repository=h.repository,
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=h.runner.compatibility,
        article_analysis_runner=LedgeredArticleAnalysisRunner(
            h.repository, h.service, h.runner.compatibility
        ),
    )
    app = create_app(
        services=Services(
            repository=h.repository,
            constituents=FakeConstituents(),
            analysis=service,
            article_events=h.runner,
        )
    )

    with TestClient(app) as client:
        client.post("/api/v1/analyze", json={"symbol": "ACME"}).raise_for_status()
        assert h.provider.stage_a_calls == 1
        h.repository.upsert_articles([article(2, now - timedelta(hours=4))])
        stored = h.repository.latest_contract_analysis(primary.fingerprint, h.runner.compatibility)
        assert h.service.current_evidence_fingerprint(primary.fingerprint) != (
            stored.evidence_fingerprint
        )
        second = client.post("/api/v1/analyze", json={"symbol": "ACME"})

    second.raise_for_status()
    assert h.provider.stage_a_calls == 1
    assert second.json()["automatic_analysis"]["cached"] >= 1
    # The job keeps the record of the one paid attempt that produced its analysis.
    assert (h.job(primary).state, h.job(primary).reason, h.job(primary).attempts) == (
        "analyzed",
        "generated",
        1,
    )


def test_only_an_explicit_article_request_may_retry_a_permanently_failed_job(writable_tmp_path):
    provider = ScriptedProvider(
        failures=[
            ArticleAnalysisSemanticValidationError("x"),
            ArticleAnalysisSemanticValidationError("x"),
        ]
    )
    h = build(writable_tmp_path, provider)
    item = article(1, T0)
    h.repository.upsert_articles([item])
    h.runner.analyze_article(item.fingerprint)
    h.clock.now = T0 + timedelta(minutes=16)
    h.runner.analyze_article(item.fingerprint)
    assert h.job(item).state == "failed"

    refused = h.runner.analyze_article(item.fingerprint)
    assert (refused.status, refused.failure_category) == ("failed", "ledger_failed")
    assert provider.stage_a_calls == 2

    manual = LedgeredArticleAnalysisRunner(
        h.repository, h.service, h.runner.compatibility, allow_terminal_retry=True, clock=h.clock
    )
    with TestClient(
        create_app(
            services=Services(
                repository=h.repository,
                constituents=FakeConstituents(),
                analysis=object(),
                article_events=manual,
            )
        )
    ) as client:
        response = client.post("/api/v1/articles/analyze", json={"article_id": item.fingerprint})

    assert response.json()["status"] == "generated"
    assert (h.job(item).state, h.job(item).attempts) == ("analyzed", 3)


def test_build_services_wires_both_private_spending_paths_through_the_ledger(writable_tmp_path):
    settings = Settings(
        _env_file=None, database_path=writable_tmp_path / "wired.db", llm_api_key="test-key"
    )

    services = build_services(settings)

    automatic = services.analysis.article_analysis_runner
    assert isinstance(automatic, LedgeredArticleAnalysisRunner)
    assert automatic.allow_terminal_retry is False
    assert isinstance(services.article_events, LedgeredArticleAnalysisRunner)
    assert services.article_events.allow_terminal_retry is True


def test_cli_activation_and_status_resolve_companies_offline(writable_tmp_path):
    settings = Settings(_env_file=None, database_path=writable_tmp_path / "cli.db")

    offline = build_coverage_service(settings, offline=True)
    online = build_coverage_service(settings, offline=False)

    assert isinstance(offline.constituents, CacheOnlyConstituentResolver)
    assert not isinstance(online.constituents, CacheOnlyConstituentResolver)
    assert [source.name for source in offline.sources] == ["gdelt", "google_news_rss"]
    arguments = build_parser().parse_args(["--ticker", "NVDA", "--ticker", "PFE"])
    assert (arguments.mode, arguments.ticker, arguments.max_new) == ("cycle", ["NVDA", "PFE"], 10)
