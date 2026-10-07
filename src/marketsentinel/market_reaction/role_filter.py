"""Deterministic company-role eligibility rule for `mr-v1`, in two placements behind one switch.

The role label is *extraction*: a stored fact about what an article says (the covered company is its
principal subject, or only mentioned). Whether an article then counts is *this* rule, in code. The
only rule implemented is ``principal_only``: an article counts when its label is ``principal``.

Pure and deterministic, like the rest of the engine: the labels are plain input data, and nothing
here performs I/O, reads a clock, or calls anything.

An article with no label is never silently classified. It is its own category, reported as
``articles_unlabelled``, and handled by an explicit :class:`UnlabelledPolicy`.

Both placements are implemented and tested; choosing between them is a methodology decision that
belongs to the proposal (docs/research/MR-006-proposal.md), not to this module.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from marketsentinel.article_sources import source_organization
from marketsentinel.domain import CompanyRole
from marketsentinel.market_reaction.calendars import SessionCalendar
from marketsentinel.market_reaction.models import (
    MIN_DISTINCT_SOURCES,
    ReactionArticle,
    RoleFilterPlacement,
    RoleFilterReport,
    SessionSignal,
    UnlabelledPolicy,
)
from marketsentinel.market_reaction.signal import (
    SessionSignals,
    SignalDiagnostics,
    build_session_signals,
)


@dataclass(frozen=True)
class RoleFilter:
    """Role labels keyed by article ID, plus the two explicit choices that make the rule exact.

    ``roles`` holds only labelled articles. An article ID absent from it is *unlabelled*, which is
    deliberately distinct from ``CompanyRole.MENTIONED``. Supplying a filter always applies it,
    even with no labels at all (every article is then reported unlabelled); a caller that wants the
    unfiltered result passes no filter.
    """

    roles: Mapping[str, CompanyRole]
    placement: RoleFilterPlacement
    unlabelled: UnlabelledPolicy


@dataclass(frozen=True)
class RoleFilteredSignals:
    signals: SessionSignals
    report: RoleFilterReport
    # Sessions that may not become events (``at_qualification`` only; empty otherwise).
    blocked_sessions: frozenset[date]


def build_role_filtered_signals(
    articles: Iterable[ReactionArticle],
    ticker: str,
    calendar: SessionCalendar | None,
    role_filter: RoleFilter,
) -> RoleFilteredSignals:
    """Session signals under the role rule, plus the report of what the rule did.

    Under ``before_signals`` the returned signals are built from eligible articles only, so a caller
    that pools them across companies for threshold selection gets a role-filtered population ``E``.
    Under ``at_qualification`` the signals are exactly ``build_session_signals`` on every article
    (so ``E`` is unchanged) and the blocked sessions are returned for the event-qualification step.
    """

    article_list = list(articles)
    counts = _role_counts(article_list, ticker, role_filter)
    if role_filter.placement is RoleFilterPlacement.BEFORE_SIGNALS:
        return _before_signals(article_list, ticker, calendar, role_filter, counts)
    return _at_qualification(article_list, ticker, calendar, role_filter, counts)


def finalize_qualification_report(
    result: RoleFilteredSignals, sessions_removed: int
) -> RoleFilterReport:
    """Fill in how many would-be events the rule blocked (known only once thresholds are)."""

    return result.report.model_copy(update={"sessions_removed": sessions_removed})


def unfiltered_role_report(
    articles: Iterable[ReactionArticle], ticker: str, role_filter: RoleFilter
) -> RoleFilterReport:
    """The report for a company that never reached signal construction (no articles removed)."""

    counts = _role_counts(list(articles), ticker, role_filter)
    return _report(role_filter, counts, excluded=0, sessions_removed=0)


@dataclass(frozen=True)
class _Counts:
    considered: int
    principal: int
    mentioned: int
    unlabelled: int
    unlabelled_ids: frozenset[str]
    principal_ids: frozenset[str]
    mentioned_ids: frozenset[str]


def _considered(articles: Sequence[ReactionArticle], ticker: str) -> list[ReactionArticle]:
    return [
        article
        for article in articles
        if article.ticker.casefold() == ticker.casefold() and not article.is_demo
    ]


def _role_counts(
    articles: Sequence[ReactionArticle], ticker: str, role_filter: RoleFilter
) -> _Counts:
    principal: set[str] = set()
    mentioned: set[str] = set()
    unlabelled: set[str] = set()
    considered = _considered(articles, ticker)
    for article in considered:
        role = role_filter.roles.get(article.article_id)
        if role is CompanyRole.PRINCIPAL:
            principal.add(article.article_id)
        elif role is CompanyRole.MENTIONED:
            mentioned.add(article.article_id)
        else:
            unlabelled.add(article.article_id)
    return _Counts(
        considered=len(considered),
        principal=len(principal),
        mentioned=len(mentioned),
        unlabelled=len(unlabelled),
        unlabelled_ids=frozenset(unlabelled),
        principal_ids=frozenset(principal),
        mentioned_ids=frozenset(mentioned),
    )


def _report(
    role_filter: RoleFilter, counts: _Counts, *, excluded: int, sessions_removed: int
) -> RoleFilterReport:
    return RoleFilterReport(
        placement=role_filter.placement,
        unlabelled_policy=role_filter.unlabelled,
        articles_considered=counts.considered,
        articles_principal=counts.principal,
        articles_mentioned=counts.mentioned,
        articles_unlabelled=counts.unlabelled,
        articles_excluded=excluded,
        sessions_removed=sessions_removed,
    )


def _before_signals(
    articles: list[ReactionArticle],
    ticker: str,
    calendar: SessionCalendar | None,
    role_filter: RoleFilter,
    counts: _Counts,
) -> RoleFilteredSignals:
    """Placement (a): articles failing the rule never reach session-signal construction."""

    policy = role_filter.unlabelled
    # What is removed from signal construction: every mentioned article; an unlabelled one unless
    # the policy is `include`. (`session_ineligible` also removes the article, and then drops its
    # whole session below.)
    removed = set(counts.mentioned_ids)
    if policy is not UnlabelledPolicy.INCLUDE:
        removed |= counts.unlabelled_ids
    kept = [article for article in articles if article.article_id not in removed]

    if calendar is None:
        empty = SessionSignals(signals=(), diagnostics=SignalDiagnostics(len(kept)))
        return RoleFilteredSignals(
            empty,
            _report(role_filter, counts, excluded=len(removed), sessions_removed=0),
            frozenset(),
        )

    built = build_session_signals(kept, ticker, calendar)
    sessions_removed = 0
    if policy is UnlabelledPolicy.SESSION_INELIGIBLE and counts.unlabelled_ids:
        # Which sessions would hold an unlabelled article? Build once with unlabelled articles
        # included (mentioned still removed) and look at the sessions they land in.
        with_unlabelled = build_session_signals(
            [a for a in articles if a.article_id not in counts.mentioned_ids], ticker, calendar
        )
        poisoned = {
            signal.session
            for signal in with_unlabelled.signals
            if any(article_id in counts.unlabelled_ids for article_id in signal.article_ids)
        }
        surviving = tuple(s for s in built.signals if s.session not in poisoned)
        sessions_removed = len(built.signals) - len(surviving)
        built = SessionSignals(signals=surviving, diagnostics=built.diagnostics)
    return RoleFilteredSignals(
        built,
        _report(role_filter, counts, excluded=len(removed), sessions_removed=sessions_removed),
        frozenset(),
    )


def _at_qualification(
    articles: list[ReactionArticle],
    ticker: str,
    calendar: SessionCalendar | None,
    role_filter: RoleFilter,
    counts: _Counts,
) -> RoleFilteredSignals:
    """Placement (b): signals and `E` untouched; the rule gates which tail sessions become events.

    A session qualifies only when it still has at least ``MIN_DISTINCT_SOURCES`` distinct source
    organisations among articles that count under the rule. Under ``session_ineligible`` a session
    holding any unlabelled article does not qualify at all.
    """

    excluded = len(counts.mentioned_ids) + (
        0 if role_filter.unlabelled is UnlabelledPolicy.INCLUDE else len(counts.unlabelled_ids)
    )
    report = _report(role_filter, counts, excluded=excluded, sessions_removed=0)
    if calendar is None:
        return RoleFilteredSignals(
            SessionSignals(signals=(), diagnostics=SignalDiagnostics(len(articles))),
            report,
            frozenset(),
        )
    built = build_session_signals(articles, ticker, calendar)
    by_id = {article.article_id: article for article in articles}
    blocked = frozenset(
        signal.session
        for signal in built.signals
        if not _qualifies(signal, by_id, role_filter, counts)
    )
    return RoleFilteredSignals(built, report, blocked)


def _qualifies(
    signal: SessionSignal,
    by_id: Mapping[str, ReactionArticle],
    role_filter: RoleFilter,
    counts: _Counts,
) -> bool:
    policy = role_filter.unlabelled
    organisations: set[str] = set()
    for article_id in signal.article_ids:
        if article_id in counts.unlabelled_ids:
            if policy is UnlabelledPolicy.SESSION_INELIGIBLE:
                return False
            if policy is UnlabelledPolicy.EXCLUDE:
                continue
        elif article_id not in counts.principal_ids:
            continue  # mentioned: does not count toward the principal source requirement
        article = by_id[article_id]
        organisations.add(source_organization(article.source, article.url, article.title))
    return len(organisations) >= MIN_DISTINCT_SOURCES
