"""The company-role stage under the job ledger and the coverage cycle. Fully offline.

Throwaway databases, a scripted provider, and a static sentiment backend: no network, no provider,
no live database, nothing read from outside tests/.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_constituent
from role_support import ScriptedRoleProvider, stored_labels

from marketsentinel.analysis_ledger import LedgeredArticleAnalysisRunner
from marketsentinel.company_role import (
    CompanyRoleService,
    UnavailableCompanyRoleProvider,
)
from marketsentinel.company_role_ledger import (
    LedgeredCompanyRoleRunner,
    RoleBudget,
    is_new_article,
    priority_article_ids,
    reconcile_role_jobs,
    role_processing_order,
)
from marketsentinel.coverage_cycle import CoverageCycleService
from marketsentinel.domain import (
    Article,
    CompanyReference,
    CompanyRole,
    CompanyRoleLabel,
    UniverseResult,
)
from marketsentinel.errors import ArticleAnalysisProviderError, ConstituentNotFoundError
from marketsentinel.event_analysis import (
    ArticleEventAnalysisService,
    UnavailableArticleAnalysisProvider,
)
from marketsentinel.normalization import article_fingerprint
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.storage.sqlite import SQLiteRepository

# Tuesday, noon UTC. The live window is 30 days, so the cut-off between "new" and "backfill" is
# 2026-08-02 12:00.
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NAMES = {"ACME": "Acme Corporation", "OTHER": "Other Inc"}


class Constituents:
    def resolve(self, symbol: str):
        if symbol not in NAMES:
            raise ConstituentNotFoundError(symbol)
        return make_constituent().model_copy(
            update={"symbol": symbol, "yahoo_symbol": symbol, "name": NAMES[symbol]}
        )

    def load(self) -> UniverseResult:
        return UniverseResult(
            constituents=[self.resolve(s) for s in NAMES],
            source="test",
            is_fallback=False,
            fetched_at=T0,
        )


def story(
    key: str, published_at: datetime, *, source: str | None = None, ticker: str = "ACME"
) -> Article:
    title = f"{NAMES[ticker]} story {key}"
    source = source or f"Wire {key}"
    return Article(
        fingerprint=article_fingerprint(title, ticker, source, published_at),
        ticker=ticker,
        title=title,
        url=f"https://wire.example/{ticker.lower()}-{key}",
        source=source,
        published_at=published_at,
        fetched_at=T0,
        provider="test-provider",
        relevance_score=0.9,
    )


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Harness:
    repository: SQLiteRepository
    provider: ScriptedRoleProvider
    cycle: CoverageCycleService
    clock: Clock

    @property
    def role_contract(self) -> str:
        return self.cycle.role_runner.contract_key

    def add(self, *articles: Article) -> None:
        self.repository.upsert_articles(articles)
        self.repository.upsert_sentiments(StaticSentimentAnalyzer().score(list(articles)))

    def role_jobs(self, ticker: str = "ACME", states=None):
        return self.repository.list_analysis_jobs(ticker, self.role_contract, states)

    def run(self, budget: RoleBudget | None, *, now: datetime | None = None, ticker: str = "ACME"):
        if now is not None:
            self.clock.now = now  # the runner keeps its own clock, like production
        return self.cycle.run(
            ticker,
            now=now or self.clock.now,
            max_new_analyses=0,
            ingest=False,
            role_budget=budget,
        )


def build(tmp_path, provider=None, tickers=("ACME",)) -> Harness:
    repository = SQLiteRepository(tmp_path / "role-ledger.db")
    repository.initialize()
    provider = provider or ScriptedRoleProvider()
    clock = Clock()
    constituents = Constituents()
    stage_a = ArticleEventAnalysisService(
        repository=repository,
        provider=UnavailableArticleAnalysisProvider(),
        constituents=constituents,
    )
    cycle = CoverageCycleService(
        constituents=constituents,
        sources=(),
        sentiment=StaticSentimentAnalyzer(),
        repository=repository,
        runner=LedgeredArticleAnalysisRunner(
            repository, stage_a, stage_a.compatibility, clock=clock
        ),
        role_runner=LedgeredCompanyRoleRunner(
            repository,
            CompanyRoleService(repository, provider, constituents, clock=clock),
            clock=clock,
        ),
    )
    for ticker in tickers:
        repository.activate_company_coverage(ticker, started_at=T0, live_window_days=30)
    return Harness(repository, provider, cycle, clock)


def new_stories(count: int, ticker: str = "ACME") -> list[Article]:
    # One article per weekday in August 2026, all inside the live window.
    days = [
        datetime(2026, 8, day, 15, 0, tzinfo=UTC) for day in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14)
    ]
    return [story(f"n{number}", days[number], ticker=ticker) for number in range(count)]


def old_stories(count: int, ticker: str = "ACME") -> list[Article]:
    # Before the cut-off: stored history, i.e. backfill.
    days = [datetime(2026, 7, day, 15, 0, tzinfo=UTC) for day in (6, 7, 8, 9, 10, 13, 14, 15)]
    return [story(f"b{number}", days[number], ticker=ticker) for number in range(count)]


# --- defaults spend nothing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "budget",
    [
        None,
        RoleBudget(),
        RoleBudget(max_new_per_ticker=5),  # needs the total as well
        RoleBudget(max_new_total=5),
    ],
)
def test_zero_defaults_and_partial_caps_spend_nothing_and_create_no_job(writable_tmp_path, budget):
    h = build(writable_tmp_path)
    h.add(*new_stories(3), *old_stories(3))

    report = h.run(budget)

    assert h.provider.calls == 0
    assert h.role_jobs() == []
    assert report.roles is None
    assert stored_labels(h.repository) == []


def test_the_budget_rejects_negative_caps_and_reports_whether_it_is_enabled():
    with pytest.raises(ValueError):
        RoleBudget(max_backfill_total=-1)
    assert not RoleBudget().enabled
    assert not RoleBudget(max_new_per_ticker=3).enabled
    assert RoleBudget(max_new_per_ticker=3, max_new_total=3).enabled
    assert RoleBudget(max_backfill_total=1).enabled


def test_a_cycle_without_a_role_runner_never_touches_the_role_stage(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(2))
    h.cycle.role_runner = None

    report = h.run(RoleBudget(max_new_per_ticker=5, max_new_total=5, max_backfill_total=5))

    assert report.roles is None
    assert h.provider.calls == 0


# --- caps, pending work, and paying once --------------------------------------------------------


def test_the_new_article_caps_are_respected_and_the_rest_stays_pending(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(6))

    report = h.run(RoleBudget(max_new_per_ticker=2, max_new_total=2))

    roles = report.roles
    assert (roles.paid_attempts, roles.generated) == (2, 2)
    assert (roles.claimable_new, roles.deferred_new, roles.stop_reason) == (6, 4, "budget")
    assert h.provider.calls == 2
    assert len(h.role_jobs(states=("analyzed",))) == 2
    # Budget-limited work is pending, never skipped, failed, or marked done.
    assert len(h.role_jobs(states=("pending",))) == 4
    assert len(stored_labels(h.repository)) == 2


def test_the_report_carries_cumulative_ledger_tokens_and_states(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(3))

    first = h.run(RoleBudget(max_new_per_ticker=2, max_new_total=2)).roles
    second = h.run(RoleBudget(max_new_per_ticker=2, max_new_total=2)).roles

    # The scripted provider reports (300, 40) tokens per paid call.
    assert (first.ledger_input_tokens, first.ledger_output_tokens) == (600, 80)
    assert first.ledger_states == {"analyzed": 2, "pending": 1}
    assert (second.ledger_input_tokens, second.ledger_output_tokens) == (900, 120)
    assert second.ledger_states == {"analyzed": 3}


def test_the_backfill_cap_is_separate_from_the_new_article_cap(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(4), *old_stories(5))

    roles = h.run(RoleBudget(max_new_per_ticker=1, max_new_total=1, max_backfill_total=3)).roles

    assert roles.paid_attempts == 4  # 1 new + 3 backfill
    assert (roles.claimable_new, roles.claimable_backfill) == (4, 5)
    assert (roles.deferred_new, roles.deferred_backfill) == (3, 2)
    labelled = {label.article_id for label in stored_labels(h.repository)}
    old_ids = {article.fingerprint for article in old_stories(5)}
    assert len(labelled & old_ids) == 3
    assert len(labelled - old_ids) == 1


def test_a_backfill_only_run_leaves_new_articles_alone(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(3), *old_stories(3))

    roles = h.run(RoleBudget(max_backfill_total=10)).roles

    assert roles.paid_attempts == 3
    assert roles.deferred_new == 3 and roles.stop_reason == "budget"
    new_ids = {article.fingerprint for article in new_stories(3)}
    assert not ({label.article_id for label in stored_labels(h.repository)} & new_ids)


def test_an_article_is_never_paid_for_twice_across_runs(writable_tmp_path):
    h = build(writable_tmp_path)
    articles = [*new_stories(3), *old_stories(3)]
    h.add(*articles)
    budget = RoleBudget(max_new_per_ticker=10, max_new_total=10, max_backfill_total=10)

    first = h.run(budget).roles
    second = h.run(budget).roles
    third = h.run(budget).roles

    assert first.paid_attempts == 6
    assert second.paid_attempts == third.paid_attempts == 0
    assert second.claimable_new == second.claimable_backfill == 0
    assert h.provider.calls == 6
    assert len({request.title for request in h.provider.requests}) == 6


def test_a_stored_label_completes_a_pending_job_for_free(writable_tmp_path):
    h = build(writable_tmp_path)
    article = new_stories(1)[0]
    h.add(article)
    service = h.cycle.role_runner.service
    contract = service.contract
    # A label already exists (for example stored by a process that died before its job moved).
    h.repository.store_company_role(
        CompanyRoleLabel(
            article_id=article.fingerprint,
            subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
            role=CompanyRole.MENTIONED,
            confidence=0.8,
            rationale="Already stored.",
            model_version=contract.model_version,
            prompt_version=contract.prompt_version,
            schema_version=contract.schema_version,
            created_at=T0,
        )
    )

    roles = h.run(RoleBudget(max_new_per_ticker=5, max_new_total=5)).roles

    assert (roles.paid_attempts, roles.reused) == (0, 1)
    assert h.provider.calls == 0
    job = h.role_jobs()[0]
    assert (job.state, job.reason, job.attempts) == ("analyzed", "preexisting", 0)


# --- failures retry under the ledger's rules ----------------------------------------------------


def test_a_transient_failure_retries_after_the_ledger_backoff(writable_tmp_path):
    provider = ScriptedRoleProvider(failures=[ArticleAnalysisProviderError("timeout")])
    h = build(writable_tmp_path, provider)
    h.add(*new_stories(1))
    budget = RoleBudget(max_new_per_ticker=5, max_new_total=5)

    first = h.run(budget).roles
    job = h.role_jobs()[0]
    assert first.paid_attempts == 1 and first.retry_scheduled == 1 and first.generated == 0
    assert job.state == "retry_wait"
    assert job.next_attempt_at == T0 + timedelta(minutes=15)
    assert stored_labels(h.repository) == []

    not_yet = h.run(budget, now=T0 + timedelta(minutes=5)).roles
    assert not_yet.paid_attempts == 0 and provider.calls == 1

    due = h.run(budget, now=T0 + timedelta(minutes=16)).roles
    assert (due.paid_attempts, due.generated) == (1, 1)
    assert provider.calls == 2
    assert h.role_jobs()[0].state == "analyzed"


def test_a_semantic_validation_failure_retries_once_then_fails_permanently(writable_tmp_path):
    h = build(writable_tmp_path, ScriptedRoleProvider(symbol="OTHER"))  # wrong company, every time
    h.add(*new_stories(1))
    budget = RoleBudget(max_new_per_ticker=5, max_new_total=5)

    h.run(budget)
    assert h.role_jobs()[0].state == "retry_wait"
    h.run(budget, now=T0 + timedelta(minutes=16))

    job = h.role_jobs()[0]
    assert job.state == "failed" and job.last_failure_category == "semantic_validation"
    assert stored_labels(h.repository) == []
    # A permanent failure is terminal for the automatic pass: it is not paid for again.
    h.run(budget, now=T0 + timedelta(days=2))
    assert h.provider.calls == 2


def test_two_consecutive_paid_failures_trip_the_circuit_breaker(writable_tmp_path):
    h = build(
        writable_tmp_path, ScriptedRoleProvider(always=ArticleAnalysisProviderError("timeout"))
    )
    h.add(*new_stories(5))

    roles = h.run(RoleBudget(max_new_per_ticker=5, max_new_total=5)).roles

    assert roles.paid_attempts == 2 and roles.stop_reason == "circuit_breaker"
    assert roles.deferred_new == 3
    assert len(h.role_jobs(states=("pending",))) == 3


def test_an_unavailable_provider_stops_without_spending_or_marking_anything_done(writable_tmp_path):
    h = build(writable_tmp_path, UnavailableCompanyRoleProvider())
    h.add(*new_stories(3), *old_stories(2))

    roles = h.run(RoleBudget(max_new_per_ticker=5, max_new_total=5, max_backfill_total=5)).roles

    assert roles.paid_attempts == 0 and roles.stop_reason == "provider_unavailable"
    assert roles.deferred_new == 3 and roles.deferred_backfill == 2
    assert {job.state for job in h.role_jobs()} == {"pending"}
    assert all(job.attempts == 0 for job in h.role_jobs())


def test_a_demo_article_is_skipped_by_the_only_skip_rule(writable_tmp_path):
    h = build(writable_tmp_path)
    demo = story("demo", datetime(2026, 8, 5, 15, 0, tzinfo=UTC)).model_copy(
        update={"is_demo": True}
    )
    h.add(demo, *new_stories(1))

    h.run(RoleBudget(max_new_per_ticker=5, max_new_total=5))

    assert h.repository.get_analysis_job(demo.fingerprint, h.role_contract).state == "skipped"
    assert all(label.article_id != demo.fingerprint for label in stored_labels(h.repository))


# --- ordering and origin ------------------------------------------------------------------------


def corpus_with_one_priority_session() -> list[Article]:
    # 2026-08-10: three distinct sources in one session -> can change an `mr-v1` result.
    priority = [
        story(
            f"p{number}",
            datetime(2026, 8, 10, 15, 5 * number, tzinfo=UTC),
            source=f"Wire P{number}",
        )
        for number in range(3)
    ]
    # Newer lone articles, one source per session: they cannot become events.
    lone = [
        story("lone1", datetime(2026, 8, 14, 15, 0, tzinfo=UTC)),
        story("lone2", datetime(2026, 8, 20, 15, 0, tzinfo=UTC)),
    ]
    return [*priority, *lone]


def test_articles_in_sessions_with_three_sources_are_labelled_first(writable_tmp_path):
    h = build(writable_tmp_path)
    articles = corpus_with_one_priority_session()
    h.add(*articles)

    h.run(RoleBudget(max_new_per_ticker=3, max_new_total=3))

    labelled = {label.article_id for label in stored_labels(h.repository)}
    assert labelled == {a.fingerprint for a in articles if "story p" in a.title}


def test_priority_is_exactly_the_engines_three_source_sessions(writable_tmp_path):
    h = build(writable_tmp_path)
    articles = corpus_with_one_priority_session()
    h.add(*articles)

    ids = priority_article_ids(
        h.repository.list_scored_articles("ACME", limit=None), make_constituent().market
    )

    assert ids == {a.fingerprint for a in articles if "story p" in a.title}
    assert priority_article_ids([], "S&P 500") == frozenset()
    assert (
        priority_article_ids(
            h.repository.list_scored_articles("ACME", limit=None), "Unlisted Exchange"
        )
        == frozenset()
    )


def test_processing_order_is_deterministic_priority_first_then_newest(writable_tmp_path):
    articles = corpus_with_one_priority_session()
    priority = frozenset(a.fingerprint for a in articles if "story p" in a.title)

    first = role_processing_order(articles, priority)
    second = role_processing_order(list(reversed(articles)), priority)

    assert [a.fingerprint for a in first] == [a.fingerprint for a in second]
    assert {a.fingerprint for a in first[:3]} == priority
    assert [a.title for a in first[3:]] == [
        "Acme Corporation story lone2",
        "Acme Corporation story lone1",
    ]


def test_two_independent_runs_call_the_provider_in_the_same_order(writable_tmp_path):
    titles = []
    for index in range(2):
        h = build(writable_tmp_path / f"order{index}")
        h.add(*corpus_with_one_priority_session(), *old_stories(3))
        h.run(RoleBudget(max_new_per_ticker=9, max_new_total=9, max_backfill_total=9))
        titles.append([request.title for request in h.provider.requests])
    assert titles[0] == titles[1] and len(titles[0]) == 8


def test_new_versus_backfill_is_decided_from_the_article_alone():
    inside = story("in", T0 - timedelta(days=29))
    outside = story("out", T0 - timedelta(days=31))
    assert is_new_article(inside, T0, 30) and not is_new_article(outside, T0, 30)


def test_reconcile_is_idempotent_and_gives_every_article_one_job(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(2), *old_stories(2))
    contract = h.role_contract

    first = reconcile_role_jobs(h.repository, contract, "ACME", T0)
    second = reconcile_role_jobs(h.repository, contract, "ACME", T0)

    assert first == {"pending": 4} and second == {}
    assert len(h.role_jobs()) == 4


# --- the Stage A/B/C ledger is untouched --------------------------------------------------------


def test_the_stage_a_ledger_is_unaffected_by_the_role_stage(writable_tmp_path):
    h = build(writable_tmp_path)
    h.add(*new_stories(3), *old_stories(2))
    stage_a_contract = h.cycle.contract
    h.run(None)  # one cycle with the role stage off: reconciles Stage A only
    before = h.repository.analysis_job_summary("ACME", stage_a_contract)

    h.run(RoleBudget(max_new_per_ticker=10, max_new_total=10, max_backfill_total=10))
    after = h.repository.analysis_job_summary("ACME", stage_a_contract)

    assert after == before
    assert stage_a_contract != h.role_contract
    assert h.repository.analysis_job_summary("ACME", h.role_contract).states == {"analyzed": 5}


# --- several tickers share the caps fairly ------------------------------------------------------


def test_a_shared_total_is_allocated_round_robin_across_tickers(writable_tmp_path):
    h = build(writable_tmp_path, ScriptedRoleProvider(symbol=None), tickers=("ACME", "OTHER"))
    h.add(*new_stories(4), *new_stories(4, ticker="OTHER"))

    results = h.cycle.run_all(
        ["ACME", "OTHER"],
        now=T0,
        max_new_per_ticker=0,
        max_new_total=0,
        ingest=False,
        role_budget=RoleBudget(max_new_per_ticker=2, max_new_total=3),
    )

    paid = {r.ticker: r.report.roles.paid_attempts for r in results}
    assert sum(paid.values()) == 3
    assert sorted(paid.values()) == [1, 2]  # neither ticker starved, neither exceeds its own cap
    assert all(r.report.roles.stop_reason == "budget" for r in results)
