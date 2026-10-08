"""MR-009: planning and running a backfill that skips the most recent months (months 13-36).

Fully offline: scripted news, a static sentiment backend, counting providers, injected clocks and
throwaway databases. Nothing here fetches, scores with a model, or reads a real database.
"""

from datetime import UTC, datetime, timedelta

import pytest
from role_support import ScriptedRoleProvider, stored_labels
from test_backfill_service import (
    NOW,
    BucketAwareHistoricalNews,
    CountingArticleIntelligenceProvider,
    FakeConstituents,
    _service,
)

from marketsentinel.analysis_ledger import LedgeredArticleAnalysisRunner
from marketsentinel.company_role import CompanyRoleService
from marketsentinel.company_role_ledger import LedgeredCompanyRoleRunner, RoleBudget
from marketsentinel.config import Settings
from marketsentinel.coverage_cycle import CoverageCycleService
from marketsentinel.domain import Constituent
from marketsentinel.event_analysis import (
    ArticleEventAnalysisService,
    UnavailableArticleAnalysisProvider,
)
from marketsentinel.historical_backfill import BackfillRunReport, plan_backfill_buckets
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.sources.historical import (
    GdeltHistoricalNewsProvider,
    GoogleNewsHistoricalProvider,
    HistoricalNewsService,
)
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts import backfill_historical_intelligence as cli
from scripts.backfill_historical_intelligence import (
    build_backfill_service,
    horizon_days_for,
    offset_days_for,
)

RUN_DATE = datetime(2026, 10, 7, 12, tzinfo=UTC)
HORIZON = horizon_days_for(36)  # 1080
OFFSET = horizon_days_for(12)  # 360


# --- the planner ------------------------------------------------------------------------------


def test_offset_unset_plans_exactly_what_it_planned_before() -> None:
    for horizon in (30, 90, 360, 366, 1080):
        assert plan_backfill_buckets(RUN_DATE, horizon_days=horizon) == plan_backfill_buckets(
            RUN_DATE, horizon_days=horizon, offset_days=0
        )
    default = plan_backfill_buckets(NOW)
    assert default[0].start == NOW - timedelta(days=366)
    assert default[-1].end == NOW


def test_months_13_to_36_are_planned_and_nothing_more_recent() -> None:
    buckets = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON, offset_days=OFFSET)

    assert buckets[0].start == RUN_DATE - timedelta(days=HORIZON)
    assert buckets[-1].end == RUN_DATE - timedelta(days=OFFSET)
    assert buckets[0].label == "2023-10"
    assert buckets[-1].label == "2025-10"
    assert len(buckets) == 25
    # Contiguous, positive width, calendar months in between.
    for earlier, later in zip(buckets, buckets[1:], strict=False):
        assert earlier.end == later.start
    assert all(bucket.start < bucket.end for bucket in buckets)
    assert all(bucket.end <= RUN_DATE - timedelta(days=OFFSET) for bucket in buckets)


def test_the_first_and_last_bucket_are_partial_and_interior_buckets_are_whole_months() -> None:
    buckets = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON, offset_days=OFFSET)

    first, last, interior = buckets[0], buckets[-1], buckets[1:-1]
    assert first.start.day != 1 or first.start.hour != 0  # clipped to the horizon start
    assert last.end < datetime(2025, 11, 1, tzinfo=UTC)  # clipped to now - offset
    assert all(b.start.day == 1 and b.end.day == 1 for b in interior)
    assert last.start == datetime(2025, 10, 1, tzinfo=UTC)


def test_offset_buckets_use_the_boundaries_a_plain_longer_run_plans() -> None:
    full = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON)
    offset = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON, offset_days=OFFSET)

    assert offset[:-1] == full[: len(offset) - 1]
    straddling = full[len(offset) - 1]
    assert (offset[-1].label, offset[-1].start) == (straddling.label, straddling.start)
    assert offset[-1].end == RUN_DATE - timedelta(days=OFFSET) < straddling.end


def test_no_overlap_and_no_gap_against_the_most_recent_twelve_months() -> None:
    full = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON)
    deep = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON, offset_days=OFFSET)
    recent = plan_backfill_buckets(RUN_DATE, horizon_days=OFFSET)

    assert deep[-1].end == recent[0].start, "the two ranges meet exactly at the offset"
    assert deep[0].start == full[0].start
    assert recent[-1].end == full[-1].end == RUN_DATE
    # Every instant of the full horizon is covered once: the straddling month is split, not lost.
    spans = [(b.start, b.end) for b in deep + recent]
    assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:], strict=False))
    assert sum((end - start for start, end in spans), timedelta()) == timedelta(days=HORIZON)


def test_as_of_would_have_planned_the_same_buckets_but_the_cli_does_not_allow_it() -> None:
    """Section 1 of the packet: --as-of is geometrically equivalent, yet refused in backfill mode."""

    as_of = RUN_DATE - timedelta(days=OFFSET)
    via_as_of = plan_backfill_buckets(as_of, horizon_days=HORIZON - OFFSET)
    via_offset = plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON, offset_days=OFFSET)

    assert via_as_of == via_offset


def test_a_run_date_on_a_month_boundary_never_yields_a_zero_width_bucket() -> None:
    # The offset end lands exactly on 2025-11-01 00:00.
    now = datetime(2026, 10, 27, tzinfo=UTC)
    assert now - timedelta(days=OFFSET) == datetime(2025, 11, 1, tzinfo=UTC)

    buckets = plan_backfill_buckets(now, horizon_days=HORIZON, offset_days=OFFSET)

    assert buckets[-1].label == "2025-10"
    assert buckets[-1].end == datetime(2025, 11, 1, tzinfo=UTC)
    assert all(bucket.end > bucket.start for bucket in buckets)

    # And the run date itself on a month boundary, with and without an offset.
    first_of_month = datetime(2026, 10, 1, tzinfo=UTC)
    for offset in (0, OFFSET):
        planned = plan_backfill_buckets(first_of_month, HORIZON, offset)
        assert planned[-1].end == first_of_month - timedelta(days=offset)
        assert all(bucket.end > bucket.start for bucket in planned)


@pytest.mark.parametrize("offset", [HORIZON, HORIZON + 30, -1])
def test_an_offset_that_leaves_no_range_or_is_negative_is_rejected(offset: int) -> None:
    with pytest.raises(ValueError, match="offset_days"):
        plan_backfill_buckets(RUN_DATE, horizon_days=HORIZON, offset_days=offset)


def test_the_planner_is_pure_for_a_given_now() -> None:
    first = plan_backfill_buckets(RUN_DATE, HORIZON, OFFSET)
    second = plan_backfill_buckets(RUN_DATE, HORIZON, OFFSET)
    assert first == second


def test_report_names_the_skipped_range_only_when_one_was_skipped() -> None:
    def report(offset: int) -> BackfillRunReport:
        return BackfillRunReport(
            ticker="NVDA",
            horizon_days=HORIZON,
            buckets=(),
            new_analyses_attempted=0,
            circuit_breaker_tripped=False,
            sentiment_dates_total=0,
            offset_days=offset,
        )

    assert report(0).render().splitlines()[0] == (
        "Historical backfill report for NVDA (horizon: 1080 days)"
    )
    assert "skipping the most recent 360 days" in report(OFFSET).render().splitlines()[0]


# --- the CLI ----------------------------------------------------------------------------------


def test_months_convert_to_offset_days_with_the_same_thirty_day_month() -> None:
    assert offset_days_for(12, 36) == 360


@pytest.mark.parametrize(("skip", "months"), [(36, 36), (40, 36), (-1, 36)])
def test_the_cli_rejects_an_offset_that_leaves_no_month(skip: int, months: int) -> None:
    with pytest.raises(ValueError, match="skip-recent-months"):
        offset_days_for(skip, months)


@pytest.mark.parametrize(
    "extra",
    [
        ["--months", "12", "--skip-recent-months", "12"],
        ["--mode", "reanalyze-stale", "--skip-recent-months", "1", "--months", "12"],
        ["--mode", "fill-selection-gaps", "--google-only"],
        ["--mode", "refresh-evidence", "--request-interval-seconds", "2"],
        ["--request-interval-seconds", "-1"],
    ],
)
def test_the_cli_refuses_inconsistent_options_before_any_settings_or_database_work(
    extra: list[str],
) -> None:
    def must_not_be_reached(*args, **kwargs):
        raise AssertionError("must exit before settings/DB work")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(SystemExit):
        patch.setattr(
            "sys.argv", ["backfill_historical_intelligence.py", "--ticker", "NVDA", *extra]
        )
        patch.setattr(cli, "get_settings", must_not_be_reached)
        patch.setattr(cli, "build_backfill_service", must_not_be_reached)
        cli.main()


def test_the_command_for_months_13_to_36_passes_the_offset_to_the_service() -> None:
    seen: dict[str, object] = {}

    class RecordingService:
        class repository:  # noqa: N801 - mirrors the attribute the script reads
            @staticmethod
            def get_company_coverage(symbol):
                return None

        def backfill(self, ticker, *, now, horizon_days, offset_days):
            seen.update(ticker=ticker, horizon_days=horizon_days, offset_days=offset_days)

            class Report:
                @staticmethod
                def render():
                    return "rendered"

            return Report()

    def fake_build(settings, **kwargs):
        seen["build"] = kwargs
        return RecordingService()

    argv = [
        "backfill_historical_intelligence.py",
        "--ticker",
        "NVDA",
        "--months",
        "36",
        "--skip-recent-months",
        "12",
        "--google-only",
        "--max-new-analyses",
        "0",
        "--request-interval-seconds",
        "2",
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("sys.argv", argv)
        patch.setattr(cli, "get_settings", lambda: object())
        patch.setattr(cli, "build_backfill_service", fake_build)
        cli.main()

    assert seen["horizon_days"] == 1080
    assert seen["offset_days"] == 360
    assert seen["build"]["google_only"] is True
    assert seen["build"]["max_new_analyses"] == 0
    assert seen["build"]["request_interval_seconds"] == 2.0


def _settings(tmp_path, **updates) -> Settings:
    return Settings(
        database_path=tmp_path / "market.db",
        constituent_cache_path=tmp_path / "constituents.json",
        llm_api_key=None,
        **updates,
    )


def test_google_only_removes_gdelt_and_the_default_keeps_it(writable_tmp_path) -> None:
    default = build_backfill_service(
        _settings(writable_tmp_path), bucket_candidate_cap=5, max_new_analyses=60
    )
    google_only = build_backfill_service(
        _settings(writable_tmp_path),
        bucket_candidate_cap=5,
        max_new_analyses=0,
        google_only=True,
        request_interval_seconds=2.0,
    )

    assert isinstance(default.historical_news, HistoricalNewsService)
    assert isinstance(default.historical_news.primary, GdeltHistoricalNewsProvider)
    assert isinstance(default.historical_news.rss_fallback, GoogleNewsHistoricalProvider)
    assert default.historical_news.rss_fallback.request_interval_seconds == 0.0

    assert isinstance(google_only.historical_news.primary, GoogleNewsHistoricalProvider)
    assert google_only.historical_news.rss_fallback is None
    assert google_only.historical_news.primary.request_interval_seconds == 2.0


def test_a_zero_analysis_budget_wires_the_unavailable_provider_even_with_a_key(
    writable_tmp_path,
) -> None:
    with_key = _settings(writable_tmp_path).model_copy(update={"llm_api_key": "sk-test-not-real"})

    service = build_backfill_service(with_key, bucket_candidate_cap=5, max_new_analyses=0)

    assert isinstance(service.article_analysis_runner.provider, UnavailableArticleAnalysisProvider)


# --- the service ------------------------------------------------------------------------------


def test_an_offset_run_fetches_and_stores_nothing_inside_the_skipped_months(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, _provider = _service(repository, news, max_new_analyses=0)

    report = service.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)

    boundary = NOW - timedelta(days=OFFSET)
    expected = plan_backfill_buckets(NOW, horizon_days=HORIZON, offset_days=OFFSET)
    assert news.calls == [(bucket.start, bucket.end) for bucket in expected]
    assert all(until <= boundary for _, until in news.calls)
    stored = repository.list_articles("ACME", since=None)
    assert stored and max(item.published_at for item in stored) < boundary
    assert [item.bucket for item in report.buckets] == expected
    assert report.offset_days == OFFSET
    assert "skipping the most recent 360 days" in report.render()


def test_the_offset_range_and_the_recent_range_meet_without_overlap_or_gap(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, _provider = _service(repository, news, max_new_analyses=0)

    service.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)
    deep_calls = list(news.calls)
    news.calls.clear()
    service.backfill("ACME", now=NOW, horizon_days=OFFSET)
    recent_calls = list(news.calls)

    assert deep_calls[-1][1] == recent_calls[0][0]
    ranges = deep_calls + recent_calls
    assert all(left[1] <= right[0] for left, right in zip(ranges, ranges[1:], strict=False))
    assert ranges[0][0] == NOW - timedelta(days=HORIZON)
    assert ranges[-1][1] == NOW


def test_an_offset_run_with_a_zero_analysis_budget_stores_and_scores_but_never_analyses(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, provider = _service(repository, news, bucket_candidate_cap=5, max_new_analyses=0)

    report = service.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)

    assert provider.total_calls == 0
    assert report.new_analyses_attempted == 0
    assert repository.list_article_analyses("ACME", since=None, limit=1000) == []
    assert repository.list_scored_articles("ACME", limit=None), "fetch and score still ran"
    assert report.sentiment_dates_total > 0
    assert all("budget already exhausted" in (item.message or "") for item in report.buckets)


def test_rerunning_the_offset_run_adds_no_rows(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, _provider = _service(repository, news, max_new_analyses=0)

    service.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)
    first = len(repository.list_articles("ACME", since=None))
    service.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)

    assert len(repository.list_articles("ACME", since=None)) == first


def test_an_article_already_stored_at_the_boundary_is_updated_not_duplicated(
    writable_tmp_path,
) -> None:
    """The same title, source and instant fingerprints identically, so an overlap is an upsert."""

    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, _provider = _service(repository, news, max_new_analyses=0)
    service.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)
    stored = repository.list_articles("ACME", since=None)

    # A wider second run re-fetches the same buckets (and more): stored rows are not doubled.
    service.backfill("ACME", now=NOW, horizon_days=HORIZON)
    after = repository.list_articles("ACME", since=None)

    assert {item.fingerprint for item in stored} <= {item.fingerprint for item in after}
    assert len({item.fingerprint for item in after}) == len(after)


# --- what the new articles are, afterwards -----------------------------------------------------


def test_deep_history_becomes_baseline_and_waits_for_an_explicit_role_budget(
    writable_tmp_path,
) -> None:
    """Section 3: ledger state, scheduled-run behaviour and the role stage for months 13-36."""

    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    backfill, _unused = _service(repository, news, max_new_analyses=0)
    backfill.backfill("ACME", now=NOW, horizon_days=HORIZON, offset_days=OFFSET)
    stored = repository.list_articles("ACME", since=None)
    assert stored

    constituents = FakeConstituents()
    stage_a_provider = CountingArticleIntelligenceProvider()
    stage_a = ArticleEventAnalysisService(
        repository=repository, provider=stage_a_provider, constituents=constituents
    )
    role_provider = ScriptedRoleProvider()
    cycle = CoverageCycleService(
        constituents=constituents,
        sources=(),
        sentiment=StaticSentimentAnalyzer(),
        repository=repository,
        runner=LedgeredArticleAnalysisRunner(repository, stage_a, stage_a.compatibility),
        role_runner=LedgeredCompanyRoleRunner(
            repository, CompanyRoleService(repository, role_provider, constituents)
        ),
    )
    repository.activate_company_coverage("ACME", started_at=NOW, live_window_days=30)

    # A scheduled run: normal Stage A/B/C caps and the steady-state role caps, no backfill cap.
    scheduled = cycle.run(
        "ACME",
        now=NOW,
        max_new_analyses=1000,
        ingest=False,
        role_budget=RoleBudget(max_new_per_ticker=10, max_new_total=25),
    )

    assert stage_a_provider.total_calls == 0, "no paid Stage A/B/C on 24 months of old articles"
    assert scheduled.reconcile.created == {"baseline": len(stored)}
    assert role_provider.calls == 0
    assert scheduled.roles is not None
    assert scheduled.roles.claimable_new == 0
    assert scheduled.roles.claimable_backfill == len(stored)

    # Only an explicit backfill cap pays for labels, one per article and never twice.
    drained = cycle.run(
        "ACME",
        now=NOW,
        max_new_analyses=1000,
        ingest=False,
        role_budget=RoleBudget(max_backfill_total=4),
    )
    assert drained.roles is not None
    assert drained.roles.paid_attempts == 4
    assert drained.roles.deferred_backfill == len(stored) - 4
    assert len(stored_labels(repository)) == 4
    assert stage_a_provider.total_calls == 0

    again = cycle.run(
        "ACME",
        now=NOW,
        max_new_analyses=1000,
        ingest=False,
        role_budget=RoleBudget(max_backfill_total=4),
    )
    assert again.roles is not None
    assert len(stored_labels(repository)) == 8, "the next four, not the same four again"


# --- Google pacing ----------------------------------------------------------------------------

_RSS = b"""<?xml version='1.0' encoding='UTF-8'?>
<rss version='2.0'><channel><item>
  <title>Apple Inc. raises guidance - Example Finance</title>
  <link>https://news.google.com/rss/articles/one</link>
  <pubDate>Mon, 10 Aug 2026 12:00:00 GMT</pubDate>
  <source url='https://example-finance.test'>Example Finance</source>
</item><item>
  <title>Apple Inc. unveils a new data centre in Texas - Other Wire</title>
  <link>https://news.google.com/rss/articles/two</link>
  <pubDate>Tue, 11 Aug 2026 12:00:00 GMT</pubDate>
  <source url='https://other.test'>Other Wire</source>
</item></channel></rss>"""


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _Page:
    content = _RSS

    def __init__(self, url: str = "https://publisher.example/story") -> None:
        self.url = url

    def raise_for_status(self) -> None:
        return None


_APPLE = Constituent(symbol="AAPL", yahoo_symbol="AAPL", name="Apple Inc.", market="S&P 500")


def _fetch(provider: GoogleNewsHistoricalProvider):
    return provider.fetch_history(
        _APPLE,
        since=datetime(2026, 8, 1, tzinfo=UTC),
        until=datetime(2026, 8, 31, tzinfo=UTC),
        max_articles=20,
    )


def test_google_requests_including_redirect_resolutions_are_spaced_when_asked() -> None:
    clock = _Clock()
    urls: list[str] = []

    def fake_get(url, **kwargs):
        urls.append(url)
        return _Page()

    provider = GoogleNewsHistoricalProvider(
        http_get=fake_get,
        request_interval_seconds=5.0,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    result = _fetch(provider)

    assert len(result.articles) == 2
    assert len(urls) == 3, "one RSS request plus one redirect resolution per article"
    assert clock.slept == [5.0, 5.0], "the first request is immediate, each later one waits"


def test_google_requests_are_unpaced_by_default_and_the_clock_is_never_read() -> None:
    def no_clock() -> float:
        raise AssertionError("an unpaced provider must not read the clock")

    def no_sleep(_: float) -> None:
        raise AssertionError("an unpaced provider must not sleep")

    provider = GoogleNewsHistoricalProvider(
        http_get=lambda url, **kwargs: _Page(),
        sleeper=no_sleep,
        monotonic=no_clock,
    )

    assert len(_fetch(provider).articles) == 2
