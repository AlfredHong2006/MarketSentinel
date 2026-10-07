"""The company-role eligibility rule in the `mr-v1` engine: both placements, every unlabelled policy.

Pure and offline. The labels are plain input data; no label comes from a model here, and no real
return is read. Scenarios reuse the synthetic builders of tests/test_market_reaction.py, whose
events are laid out by session offset with exact, known +5 returns.
"""

import inspect
from datetime import timedelta

import pytest
from test_market_reaction import (
    ORIGIN,
    US,
    article,
    noisy,
    run,
    scenario,
)

from marketsentinel.domain import CompanyRole
from marketsentinel.market_reaction import (
    MR_V1_FROZEN_THRESHOLDS,
    EventStatus,
    EvidenceState,
    RegimeThresholds,
    RoleFilter,
    RoleFilterPlacement,
    RoleFilterReport,
    UnlabelledPolicy,
    analyze_market_reaction,
    build_role_filtered_signals,
    build_session_signals,
    select_thresholds,
)
from marketsentinel.market_reaction import role_filter as role_filter_module

BEFORE = RoleFilterPlacement.BEFORE_SIGNALS
AT = RoleFilterPlacement.AT_QUALIFICATION
PLACEMENTS = (BEFORE, AT)
POLICIES = tuple(UnlabelledPolicy)
EVENT_COUNT = 24
SPACING = 12


def event_position(number: int) -> int:
    return ORIGIN + number * SPACING


def ids_of_session(articles, position: int) -> set[str]:
    return {a.article_id for a in articles if a.article_id.startswith(f"s{position}-")}


def corpus():
    return scenario(noisy(0.03, EVENT_COUNT))


def label_all(articles, role: CompanyRole = CompanyRole.PRINCIPAL) -> dict[str, CompanyRole]:
    return {a.article_id: role for a in articles}


def apply(articles, roles, placement, policy, **kwargs):
    stock, benchmark = kwargs.pop("stock"), kwargs.pop("benchmark")
    return run(
        articles,
        stock,
        benchmark,
        role_filter=RoleFilter(roles, placement, policy),
        **kwargs,
    )


# --- no filter: nothing changes -----------------------------------------------------------------


def test_without_a_filter_the_result_is_unchanged_and_carries_no_report():
    articles, stock, benchmark = corpus()

    plain = run(articles, stock, benchmark)
    explicit_none = run(articles, stock, benchmark, role_filter=None)

    assert plain.role_filter is None
    assert plain == explicit_none
    assert plain.positive.resolved_event_count == EVENT_COUNT


def test_the_frozen_thresholds_are_still_unset():
    assert MR_V1_FROZEN_THRESHOLDS is None


# --- every article principal: the rule is a no-op, under every placement and policy ---------------


@pytest.mark.parametrize("placement", PLACEMENTS)
@pytest.mark.parametrize("policy", POLICIES)
def test_when_every_article_is_principal_the_events_are_unchanged(placement, policy):
    articles, stock, benchmark = corpus()
    baseline = run(articles, stock, benchmark)

    result = apply(
        articles, label_all(articles), placement, policy, stock=stock, benchmark=benchmark
    )

    assert result.positive.resolved_event_count == baseline.positive.resolved_event_count
    assert result.positive.primary == baseline.positive.primary
    assert result.session_signals == baseline.session_signals
    report = result.role_filter
    assert (report.articles_considered, report.articles_principal) == (len(articles), len(articles))
    assert (report.articles_mentioned, report.articles_unlabelled, report.articles_excluded) == (
        0,
        0,
        0,
    )
    assert report.sessions_removed == 0
    assert (report.placement, report.unlabelled_policy) == (placement, policy)


# --- mentioned articles: the two placements differ exactly where the spec of each says --------------


def mentioned_sessions(articles, numbers):
    roles = label_all(articles)
    for number in numbers:
        for article_id in ids_of_session(articles, event_position(number)):
            roles[article_id] = CompanyRole.MENTIONED
    return roles


def test_placement_a_removes_mentioned_sessions_before_signals_are_built():
    articles, stock, benchmark = corpus()
    baseline = run(articles, stock, benchmark)
    roles = mentioned_sessions(articles, range(5))

    result = apply(
        articles, roles, BEFORE, UnlabelledPolicy.EXCLUDE, stock=stock, benchmark=benchmark
    )

    assert result.positive.resolved_event_count == EVENT_COUNT - 5
    # The five sessions have no signal at all: they never existed for this company.
    assert len(result.session_signals) == len(baseline.session_signals) - 5
    report = result.role_filter
    assert (report.articles_mentioned, report.articles_excluded) == (15, 15)
    assert report.articles_unlabelled == 0 and report.sessions_removed == 0


def test_placement_b_keeps_every_signal_and_blocks_the_events_at_qualification():
    articles, stock, benchmark = corpus()
    baseline = run(articles, stock, benchmark)
    roles = mentioned_sessions(articles, range(5))

    result = apply(articles, roles, AT, UnlabelledPolicy.EXCLUDE, stock=stock, benchmark=benchmark)

    assert result.positive.resolved_event_count == EVENT_COUNT - 5
    assert result.session_signals == baseline.session_signals  # signals are exactly as before
    report = result.role_filter
    assert (report.articles_mentioned, report.articles_excluded) == (15, 15)
    assert report.sessions_removed == 5  # tail sessions that could not become events
    assert result.history == baseline.history  # history sufficiency is unchanged under (b)


def test_a_blocked_session_holds_no_exclusivity_window():
    # Events 4 sessions apart overlap, so normally the second is suppressed by the first. If the
    # first is blocked by the role rule it is not an event at all and must not suppress the second.
    articles, stock, benchmark = scenario(noisy(0.03, 3), spacing=4)
    first_session, second_session = US.sessions[ORIGIN], US.sessions[ORIGIN + 4]
    roles = label_all(articles)
    for article_id in ids_of_session(articles, ORIGIN):
        roles[article_id] = CompanyRole.MENTIONED

    unfiltered = run(articles, stock, benchmark)
    blocked = apply(articles, roles, AT, UnlabelledPolicy.EXCLUDE, stock=stock, benchmark=benchmark)

    before = {e.session: e.status for e in unfiltered.positive.events}
    after = {e.session: e.status for e in blocked.positive.events}
    assert before[second_session] is EventStatus.SUPPRESSED_OVERLAP
    assert first_session not in after
    assert after[second_session] is EventStatus.RESOLVED


def test_the_threshold_selection_population_changes_under_a_but_not_under_b():
    articles, _, _ = corpus()
    roles = mentioned_sessions(articles, range(5))

    plain = select_thresholds(build_session_signals(articles, "ACME", US).signals)
    before = select_thresholds(
        build_role_filtered_signals(
            articles, "ACME", US, RoleFilter(roles, BEFORE, UnlabelledPolicy.EXCLUDE)
        ).signals.signals
    )
    at = select_thresholds(
        build_role_filtered_signals(
            articles, "ACME", US, RoleFilter(roles, AT, UnlabelledPolicy.EXCLUDE)
        ).signals.signals
    )

    assert before.population_count == plain.population_count - 5
    assert at.population_count == plain.population_count
    assert at.thresholds == plain.thresholds


def test_the_session_signal_is_principal_only_under_a_and_unchanged_under_b():
    articles, stock, benchmark = corpus()
    first = event_position(0)
    # A strongly negative article from a company mentioned only in passing: it drags the plain mean
    # of this session from 0.7 down to 0.3, below a 0.5 threshold.
    intruder = article(
        f"s{first}-intruder",
        US.closes[first] - timedelta(hours=2),
        positive=0.0,
        negative=0.9,
    )
    articles = [*articles, intruder]
    roles = label_all(articles)
    roles[intruder.article_id] = CompanyRole.MENTIONED
    threshold = RegimeThresholds(negative=0.5, positive=0.5)

    plain = run(articles, stock, benchmark, thresholds=threshold)
    before = apply(
        articles,
        roles,
        BEFORE,
        UnlabelledPolicy.EXCLUDE,
        stock=stock,
        benchmark=benchmark,
        thresholds=threshold,
    )
    at = apply(
        articles,
        roles,
        AT,
        UnlabelledPolicy.EXCLUDE,
        stock=stock,
        benchmark=benchmark,
        thresholds=threshold,
    )

    def signal_at(result):
        return next(s.signal for s in result.session_signals if s.session == US.sessions[first])

    assert signal_at(plain) == pytest.approx(0.3, abs=1e-9)
    assert signal_at(at) == pytest.approx(0.3, abs=1e-9)  # (b) keeps the signal as it was
    assert signal_at(before) == pytest.approx(0.7, abs=1e-9)  # (a) recomputes it from principals
    # So (a) rescues an event that (b) and the plain run lose.
    assert before.positive.resolved_event_count == plain.positive.resolved_event_count + 1
    assert at.positive.resolved_event_count == plain.positive.resolved_event_count


# --- unlabelled articles: explicit policy, never silently mentioned ------------------------------


def with_unlabelled_member(articles, extra: int):
    """The first event session gets `extra` more principal sources, one of them unlabelled."""

    first = event_position(0)
    added = [
        article(
            f"s{first}-x{n}",
            US.closes[first] - timedelta(hours=1),
            positive=0.8,
            negative=0.1,
        )
        for n in range(extra)
    ]
    return [*articles, *added], added


@pytest.mark.parametrize(
    ("placement", "policy", "events", "removed_sessions"),
    [
        # 4 articles, one unlabelled: 3 principal sources remain without it.
        (BEFORE, UnlabelledPolicy.EXCLUDE, EVENT_COUNT, 0),
        (BEFORE, UnlabelledPolicy.INCLUDE, EVENT_COUNT, 0),
        (BEFORE, UnlabelledPolicy.SESSION_INELIGIBLE, EVENT_COUNT - 1, 1),
        (AT, UnlabelledPolicy.EXCLUDE, EVENT_COUNT, 0),
        (AT, UnlabelledPolicy.INCLUDE, EVENT_COUNT, 0),
        (AT, UnlabelledPolicy.SESSION_INELIGIBLE, EVENT_COUNT - 1, 1),
    ],
)
def test_an_unlabelled_fourth_article_under_each_policy(
    placement, policy, events, removed_sessions
):
    articles, stock, benchmark = corpus()
    articles, added = with_unlabelled_member(articles, 1)
    roles = label_all(articles)
    del roles[added[0].article_id]  # no label at all

    result = apply(articles, roles, placement, policy, stock=stock, benchmark=benchmark)

    assert result.positive.resolved_event_count == events
    report = result.role_filter
    assert report.articles_unlabelled == 1 and report.articles_mentioned == 0
    assert report.sessions_removed == removed_sessions


@pytest.mark.parametrize(
    ("placement", "policy", "events"),
    [
        # One of the session's three articles is unlabelled: without it only two sources remain.
        (BEFORE, UnlabelledPolicy.EXCLUDE, EVENT_COUNT - 1),
        (BEFORE, UnlabelledPolicy.INCLUDE, EVENT_COUNT),
        (BEFORE, UnlabelledPolicy.SESSION_INELIGIBLE, EVENT_COUNT - 1),
        (AT, UnlabelledPolicy.EXCLUDE, EVENT_COUNT - 1),
        (AT, UnlabelledPolicy.INCLUDE, EVENT_COUNT),
        (AT, UnlabelledPolicy.SESSION_INELIGIBLE, EVENT_COUNT - 1),
    ],
)
def test_an_unlabelled_member_of_a_three_source_session(placement, policy, events):
    articles, stock, benchmark = corpus()
    roles = label_all(articles)
    del roles[f"s{event_position(0)}-0"]

    result = apply(articles, roles, placement, policy, stock=stock, benchmark=benchmark)

    assert result.positive.resolved_event_count == events
    assert result.role_filter.articles_unlabelled == 1


def test_a_mentioned_member_and_an_unlabelled_member_are_different_facts():
    articles, stock, benchmark = corpus()
    first = event_position(0)
    roles = label_all(articles)
    roles[f"s{first}-0"] = CompanyRole.MENTIONED
    del roles[f"s{first}-1"]

    include = apply(
        articles, roles, AT, UnlabelledPolicy.INCLUDE, stock=stock, benchmark=benchmark
    ).role_filter
    exclude = apply(
        articles, roles, AT, UnlabelledPolicy.EXCLUDE, stock=stock, benchmark=benchmark
    ).role_filter

    assert (include.articles_mentioned, include.articles_unlabelled) == (1, 1)
    # Mentioned is excluded either way; unlabelled is excluded only under `exclude`.
    assert (include.articles_excluded, exclude.articles_excluded) == (1, 2)


def test_with_no_labels_every_article_is_reported_unlabelled_never_mentioned():
    articles, stock, benchmark = corpus()

    result = apply(articles, {}, BEFORE, UnlabelledPolicy.EXCLUDE, stock=stock, benchmark=benchmark)

    report = result.role_filter
    assert report.articles_unlabelled == len(articles) == report.articles_considered
    assert report.articles_mentioned == report.articles_principal == 0
    assert report.articles_excluded == len(articles)
    assert result.state is EvidenceState.NOT_ENOUGH_HISTORY
    assert result.positive.qualifying_event_count == 0


def test_with_no_labels_and_include_the_unfiltered_events_come_back_and_are_reported():
    articles, stock, benchmark = corpus()
    baseline = run(articles, stock, benchmark)

    result = apply(articles, {}, BEFORE, UnlabelledPolicy.INCLUDE, stock=stock, benchmark=benchmark)

    assert result.positive.resolved_event_count == baseline.positive.resolved_event_count
    assert result.role_filter.articles_unlabelled == len(articles)
    assert result.role_filter.articles_excluded == 0


# --- counting scope and degenerate inputs ---------------------------------------------------------


def test_other_tickers_and_demo_articles_are_not_counted():
    articles, stock, benchmark = corpus()
    stranger = article("x-other", US.closes[ORIGIN], ticker="OTHER")
    demo = article("x-demo", US.closes[ORIGIN], is_demo=True)

    result = apply(
        [*articles, stranger, demo],
        label_all(articles),
        AT,
        UnlabelledPolicy.EXCLUDE,
        stock=stock,
        benchmark=benchmark,
    )

    assert result.role_filter.articles_considered == len(articles)
    assert result.role_filter.articles_unlabelled == 0


def test_an_unresolved_exchange_still_reports_the_filter():
    articles, _, _ = corpus()

    result = analyze_market_reaction(
        ticker="ACME",
        listing_market="Unlisted Exchange",
        articles=articles,
        stock_prices=[],
        benchmark_prices=[],
        thresholds=RegimeThresholds(negative=0.3, positive=0.3),
        role_filter=RoleFilter({}, BEFORE, UnlabelledPolicy.EXCLUDE),
    )

    assert result.exchange is None
    assert isinstance(result.role_filter, RoleFilterReport)
    assert result.role_filter.articles_unlabelled == len(articles)


def test_building_filtered_signals_without_a_calendar_returns_counts_and_no_signals():
    articles, _, _ = corpus()

    built = build_role_filtered_signals(
        articles, "ACME", None, RoleFilter(label_all(articles), AT, UnlabelledPolicy.EXCLUDE)
    )

    assert built.signals.signals == ()
    assert built.report.articles_principal == len(articles)


def test_the_filter_module_is_pure():
    source = inspect.getsource(role_filter_module)
    for forbidden in ("import random", "import time", "datetime.now", "sqlite3", "httpx", "open("):
        assert forbidden not in source
