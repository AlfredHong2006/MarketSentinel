"""Continuous coverage for actively covered companies.

One cycle per ticker, in order:

1. **Ingest** from each news provider independently, from that provider's own watermark minus an
   overlap window, so a late-arriving article is still collected and one provider failing never
   holds back or advances another. Articles are stored and sentiment-scored *before* any watermark
   moves, so a crash can only cause a harmless re-fetch, never a gap.
2. **Reconcile** every stored article without a job into exactly one explicit ledger state.
3. **Analyse** claimable jobs through ``LedgeredArticleAnalysisRunner`` in the selector's priority
   order, under a per-run budget and the existing two-failure circuit breaker. Work the budget does
   not reach stays ``pending`` for the next cycle.
4. **Check evidence** of analysed jobs, recording whether each stored analysis's evidence pool is
   still the pool a fresh analysis would receive. Nothing is regenerated here; a changed pool is
   reported so an operator can run the explicit ``refresh-evidence`` backfill mode.

A manual, synchronous, one-shot service: no scheduler, queue, or daemon. Materiality, grouping,
ranking, and risks are untouched and stay recomputed on read.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.analysis_ledger import (
    LedgeredArticleAnalysisRunner,
    deterministic_skip_reason,
    processing_order,
)
from marketsentinel.company_role_ledger import (
    LedgeredCompanyRoleRunner,
    RoleBudget,
    is_new_article,
    priority_article_ids,
    reconcile_role_jobs,
    role_processing_order,
)
from marketsentinel.domain import (
    Article,
    Constituent,
    IngestionFunnel,
    NewsFetchResult,
    SourceHealth,
)
from marketsentinel.errors import ConstituentNotFoundError, CoverageNotActiveError
from marketsentinel.event_analysis import EVIDENCE_WINDOW_DAYS_AFTER
from marketsentinel.normalization import deduplicate_with_diagnostics, deduplication_reason
from marketsentinel.sentiment.finbert import SentimentAnalyzer
from marketsentinel.sources.historical import HistoricalNewsProvider
from marketsentinel.sources.news import NewsProvider
from marketsentinel.storage.sqlite import (
    AnalysisJobSummary,
    CompanyCoverage,
    IngestionWatermark,
    NewAnalysisJob,
    SQLiteRepository,
)

# Re-fetch this far behind a provider's watermark. Aggregators publish some items hours after
# their stated publication time; the overlap collects them, and deduplication makes the repeated
# part of the window free.
DEFAULT_OVERLAP = timedelta(hours=48)

# RecentProviderSource's own window-splitting step. A provider such as Google News RSS enforces
# its own result cap per request, with no cursor to page past it; asking it for one day at a time
# instead of the whole lookback keeps each request's own candidate pool well under that cap for a
# normal news day, so the *combined* fetch converges instead of hitting the cap on every cycle.
#
# Google's date search operators (``after:``/``before:``) are day-granularity only: appending a
# time component to either makes Google return zero results rather than a finer slice (checked
# directly against the live endpoint), so a window cannot usefully be split any narrower than one
# day through this mechanism.
DEFAULT_RECENT_WINDOW = timedelta(days=1)

# Google's own per-request result ceiling for this search endpoint: checked directly against the
# live endpoint, a one-day query and a seven-day query for the same terms both returned exactly
# 100 entries, so this is a hard server-side cap on one request, not a symptom of a narrow date
# range. A day-sized window's own request should be allowed up to this ceiling -- an interactive
# per-refresh budget sized for a flat multi-day request would otherwise needlessly truncate a
# single dense day a second time, below what Google already handed back for that one request.
# Undocumented and could change without notice; nothing here depends on it being exactly right,
# only on it being close, since a day genuinely denser than this still correctly stays `partial`.
GOOGLE_NEWS_RSS_RESULT_CAP = 100


class ConstituentResolver(Protocol):
    def resolve(self, symbol: str) -> Constituent: ...


class WatermarkedNewsSource(Protocol):
    """A news provider under its own watermark. ``name`` is the stable watermark key."""

    name: str
    max_lookback: timedelta
    max_articles: int

    def fetch(
        self, constituent: Constituent, since: datetime, until: datetime
    ) -> NewsFetchResult: ...


@dataclass(frozen=True)
class HistoricalProviderSource:
    """A date-bounded provider such as GDELT."""

    name: str
    provider: HistoricalNewsProvider
    max_lookback: timedelta
    max_articles: int

    def fetch(self, constituent: Constituent, since: datetime, until: datetime) -> NewsFetchResult:
        return self.provider.fetch_history(constituent, since, until, self.max_articles)


@dataclass(frozen=True)
class RecentProviderSource:
    """A recent-news provider such as Google News RSS, windowed against its own result cap.

    The provider enforces a cap per request with no cursor to page past it, so one flat request
    over a wide, high-volume span hits that cap every cycle and never converges (Google returns
    its own top matches for the query, not a complete list). Splitting ``[since, until]`` into
    ``window``-sized, non-overlapping, date-bounded requests gives each slice its own share of the
    cap instead: a slice still hitting its own cap is rare (it means that one slice alone was too
    dense), and only then does the combined result stay ``partial``.

    Each window's own request is allowed up to ``GOOGLE_NEWS_RSS_RESULT_CAP`` articles even when
    ``max_articles`` (an interactive-refresh budget shared with other callers) is smaller: that
    budget was sized for one flat request over the whole lookback, and reusing it unchanged per
    day would silently re-impose the same cap this class exists to get past. ``max_articles``
    still raises the per-window request further for a caller that explicitly wants more.
    """

    name: str
    provider: NewsProvider
    max_lookback: timedelta
    max_articles: int
    window: timedelta = DEFAULT_RECENT_WINDOW

    def fetch(self, constituent: Constituent, since: datetime, until: datetime) -> NewsFetchResult:
        window_cap = max(self.max_articles, GOOGLE_NEWS_RSS_RESULT_CAP)
        return _merge_fetch_results(
            self.provider.fetch(constituent, chunk_since, window_cap, until=chunk_until)
            for chunk_since, chunk_until in _fetch_windows(since, until, self.window)
        )


def _fetch_windows(
    since: datetime, until: datetime, step: timedelta
) -> list[tuple[datetime, datetime]]:
    """Split ``[since, until]`` into contiguous, non-overlapping ``step``-sized windows.

    Pure. Oldest window first. A span shorter than ``step`` yields exactly one window; a span of
    zero length (``since >= until``) yields none.
    """

    windows: list[tuple[datetime, datetime]] = []
    cursor = until
    while cursor > since:
        start = max(since, cursor - step)
        windows.append((start, cursor))
        cursor = start
    return list(reversed(windows))


def _merge_fetch_results(results: Iterable[NewsFetchResult]) -> NewsFetchResult:
    """Combine independently windowed fetches into the one result ``fetch_outcome`` classifies.

    Pure. Any window's provider failure or cap hit carries through to the combined result exactly
    as it would from a single flat fetch, so the watermark-advancement contract is unchanged: the
    caller cannot tell a windowed fetch from an unwindowed one from the result alone.
    """

    results = list(results)
    if not results:
        return NewsFetchResult(
            articles=[],
            health=SourceHealth(provider="", status="healthy"),
            funnel=IngestionFunnel(),
        )
    articles = [article for result in results for article in result.articles]
    funnel = IngestionFunnel()
    for result in results:
        funnel = funnel.merged(result.funnel)
    unavailable = any(result.health.status == "unavailable" for result in results)
    degraded = any(result.health.status == "degraded" for result in results)
    status = "unavailable" if unavailable else "degraded" if degraded else "healthy"
    messages = dict.fromkeys(result.health.message for result in results if result.health.message)
    return NewsFetchResult(
        articles=articles,
        health=SourceHealth(
            provider=results[0].health.provider,
            status=status,
            records_received=sum(result.health.records_received for result in results),
            valid_records=sum(result.health.valid_records for result in results),
            latency_ms=sum(result.health.latency_ms or 0 for result in results),
            message="; ".join(messages) or None,
        ),
        funnel=funnel,
    )


def fetch_window(
    ingested_through: datetime | None,
    now: datetime,
    *,
    overlap: timedelta,
    max_lookback: timedelta,
) -> tuple[datetime, bool]:
    """Return a provider's fetch start and whether its watermark is older than it can reach.

    Pure. A provider with no watermark starts at its lookback floor. A watermark so old that the
    overlap start falls before the floor is clamped and flagged, because the uncovered span cannot
    be recovered by this provider and must be reported rather than implied complete.
    """

    floor = now - max_lookback
    if ingested_through is None:
        return floor, False
    since = ingested_through - overlap
    if since < floor:
        return floor, True
    return since, False


def fetch_outcome(result: NewsFetchResult) -> tuple[str, bool, str | None]:
    """Classify a provider result as ``(status, completed, message)``. Pure.

    Only a completed fetch advances a watermark. A provider-reported failure does not, so the next
    cycle re-covers the same span. A result that hit the provider's article cap is ``partial`` and
    does not complete either: the articles it did return are stored, but the watermark stays where
    it was so a later cycle retries the unresolved window instead of permanently skipping the older
    articles the cap cut off. Repeated partial fetches are counted as consecutive problems so a
    window the provider cannot cover within its cap stays visible.
    """

    health = result.health
    if health.status == "unavailable" or result.funnel.provider_failures > 0:
        return "failed", False, health.message or "The provider reported a failure."
    if result.funnel.request_limited > 0:
        return (
            "partial",
            False,
            f"Provider article cap reached: {result.funnel.request_limited} article(s) in this "
            "window were not retrieved; the watermark was not advanced and the window will be "
            "retried.",
        )
    if not result.articles:
        return "empty", True, health.message
    return "ok", True, health.message


@dataclass(frozen=True)
class SourceReport:
    provider: str
    window_start: datetime
    window_end: datetime
    status: str
    articles: int
    message: str | None


@dataclass(frozen=True)
class IngestReport:
    sources: tuple[SourceReport, ...] = ()
    fetched_unique: int = 0
    new_articles: int = 0
    newly_scored: int = 0


@dataclass(frozen=True)
class ReconcileReport:
    created: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisPassReport:
    claimable: int = 0
    paid_attempts: int = 0
    generated: int = 0
    reused: int = 0
    retry_scheduled: int = 0
    failed: int = 0
    skipped: int = 0
    refused: int = 0
    deferred: int = 0
    stop_reason: str | None = None


@dataclass(frozen=True)
class RolePassReport:
    """One ticker's company-role pass. Absent from a report when the stage did not run."""

    reconciled: dict[str, int] = field(default_factory=dict)
    claimable_new: int = 0
    claimable_backfill: int = 0
    paid_attempts: int = 0
    generated: int = 0
    reused: int = 0
    retry_scheduled: int = 0
    failed: int = 0
    skipped: int = 0
    refused: int = 0
    deferred_new: int = 0
    deferred_backfill: int = 0
    stop_reason: str | None = None
    # Cumulative over every run for this ticker under the role contract (from the ledger), so a
    # reviewer can compare real token use with the proposal's estimate. Not a per-run figure.
    ledger_states: dict[str, int] = field(default_factory=dict)
    ledger_input_tokens: int = 0
    ledger_output_tokens: int = 0


def normalize_role_tickers(tickers: Iterable[str] | None) -> frozenset[str] | None:
    """Upper-case, trimmed, de-duplicated role ticker list; ``None`` stays ``None`` (no list)."""

    if tickers is None:
        return None
    return frozenset(ticker.strip().upper() for ticker in tickers if ticker.strip())


@dataclass(frozen=True)
class RoleScope:
    """Which tickers the role stage was allowed to label in one run. Pure; spends nothing.

    ``in_scope`` are covered tickers on the list, ``left_out`` are covered tickers not on it (no
    paid role call, no role budget), ``not_covered`` are listed tickers with no active coverage
    (reported, label nothing, not an error).
    """

    in_scope: tuple[str, ...]
    left_out: tuple[str, ...]
    not_covered: tuple[str, ...]

    def render(self) -> str:
        def names(values: tuple[str, ...]) -> str:
            return ", ".join(values) or "none"

        return (
            f"role scope: labelled tickers={names(self.in_scope)}; "
            f"covered tickers left out={names(self.left_out)}; "
            f"listed but not covered={names(self.not_covered)}"
        )


def role_scope(covered: Iterable[str], role_tickers: Iterable[str]) -> RoleScope:
    listed = normalize_role_tickers(role_tickers) or frozenset()
    covered_set = {ticker.upper() for ticker in covered}
    return RoleScope(
        in_scope=tuple(sorted(covered_set & listed)),
        left_out=tuple(sorted(covered_set - listed)),
        not_covered=tuple(sorted(listed - covered_set)),
    )


def _require_role_tickers(
    budget: RoleBudget | None, role_tickers: frozenset[str] | None
) -> frozenset[str] | None:
    """Fail closed: a positive role cap needs an explicit, non-empty ticker list."""

    if budget is not None and budget.enabled and not role_tickers:
        raise ValueError(
            "a positive company-role cap needs an explicit role ticker list; "
            "refusing before any spend"
        )
    return role_tickers


@dataclass(frozen=True)
class EvidenceCheckReport:
    checked: int = 0
    changed: int = 0
    window_open: int = 0


@dataclass(frozen=True)
class CoverageRunResult:
    """One ticker's outcome from ``run_all``: either a completed report, or why it was skipped.

    A ticker that does not resolve or is not under active coverage is reported here rather than
    raised, so one bad ticker in a batch never loses the results already computed for the rest --
    strictly safer than letting the exception propagate mid-batch, which is what calling ``run``
    for each ticker in a loop would do.
    """

    ticker: str
    report: "CoverageReport | None" = None
    error: str | None = None


@dataclass(frozen=True)
class CoverageReport:
    ticker: str
    mode: str
    analysis_contract: str
    coverage: CompanyCoverage
    ingest: IngestReport
    reconcile: ReconcileReport
    analysis: AnalysisPassReport
    evidence: EvidenceCheckReport
    summary: AnalysisJobSummary
    watermarks: tuple[IngestionWatermark, ...]
    roles: RolePassReport | None = None

    def render(self) -> str:
        lines = [
            f"Coverage {self.mode} report for {self.ticker}",
            f"  contract: {self.analysis_contract}",
            f"  ledger started: {self.coverage.ledger_started_at.isoformat()} "
            f"(live window {self.coverage.live_window_days} days)",
        ]
        for source in self.ingest.sources:
            line = (
                f"  fetch {source.provider}: {source.status} articles={source.articles} "
                f"window={source.window_start.isoformat()}..{source.window_end.isoformat()}"
            )
            if source.message:
                line += f" ({source.message})"
            lines.append(line)
        if self.ingest.sources:
            lines.append(
                f"  ingest: unique fetched={self.ingest.fetched_unique} "
                f"new stored={self.ingest.new_articles} newly scored={self.ingest.newly_scored}"
            )
        for watermark in self.watermarks:
            through = watermark.ingested_through.isoformat() if watermark.ingested_through else "-"
            lines.append(
                f"  watermark {watermark.provider}: through={through} "
                f"last={watermark.last_status} "
                f"consecutive_failed_or_partial={watermark.consecutive_failures}"
            )
        created = ", ".join(f"{k}={v}" for k, v in sorted(self.reconcile.created.items()))
        lines.append(f"  new ledger rows: {created or 'none'}")
        a = self.analysis
        lines.append(
            f"  analysis pass: claimable={a.claimable} paid_attempts={a.paid_attempts} "
            f"generated={a.generated} reused={a.reused} retry_scheduled={a.retry_scheduled} "
            f"failed={a.failed} skipped={a.skipped} refused={a.refused} deferred={a.deferred}"
            + (f" stopped={a.stop_reason}" if a.stop_reason else "")
        )
        if self.roles is not None:
            r = self.roles
            lines.append(
                f"  role stage: claimable_new={r.claimable_new} "
                f"claimable_backfill={r.claimable_backfill} paid_attempts={r.paid_attempts} "
                f"generated={r.generated} reused={r.reused} "
                f"retry_scheduled={r.retry_scheduled} failed={r.failed} skipped={r.skipped} "
                f"refused={r.refused} deferred_new={r.deferred_new} "
                f"deferred_backfill={r.deferred_backfill}"
                + (f" stopped={r.stop_reason}" if r.stop_reason else "")
            )
            states = ", ".join(f"{k}={v}" for k, v in sorted(r.ledger_states.items()))
            lines.append(
                f"  role ledger: {states or 'none'} input_tokens={r.ledger_input_tokens} "
                f"output_tokens={r.ledger_output_tokens} (cumulative)"
            )
        e = self.evidence
        lines.append(
            f"  evidence: checked={e.checked} changed_since_analysis={e.changed} "
            f"window_still_open={e.window_open}"
            + (
                " -> run the refresh-evidence backfill mode to regenerate changed evidence"
                if e.changed
                else ""
            )
        )
        s = self.summary
        states = ", ".join(f"{k}={v}" for k, v in sorted(s.states.items()))
        lines.append(f"  ledger states: {states or 'none'}")
        lines.append(
            f"  ledger totals: attempts={s.attempts} failures={s.failures} "
            f"input_tokens={s.input_tokens} output_tokens={s.output_tokens} "
            f"evidence_changed={s.evidence_changed}"
        )
        lines.append(f"  invariant: articles without a ledger row = {s.articles_without_job}")
        return "\n".join(lines)


@dataclass
class _TickerQueue:
    """One ticker's mutable state during ``_analyze_many``'s round-robin allocation."""

    ordered: list[Article] = field(default_factory=list)
    position: int = 0
    paid: int = 0
    consecutive_failures: int = 0
    stop_reason: str | None = None
    counts: Counter[str] = field(default_factory=Counter)


@dataclass
class _RoleQueue:
    """One ticker's role-stage state: new and backfill queues, each with its own position."""

    reconciled: dict[str, int]
    new: list[Article]
    backfill: list[Article]
    positions: dict[str, int] = field(default_factory=lambda: {"new": 0, "backfill": 0})
    paid: dict[str, int] = field(default_factory=lambda: {"new": 0, "backfill": 0})
    consecutive_failures: int = 0
    stop_reason: str | None = None
    counts: Counter[str] = field(default_factory=Counter)

    def items(self, phase: str) -> list[Article]:
        return self.new if phase == "new" else self.backfill


class CoverageCycleService:
    def __init__(
        self,
        *,
        constituents: ConstituentResolver,
        sources: Sequence[WatermarkedNewsSource],
        sentiment: SentimentAnalyzer,
        repository: SQLiteRepository,
        runner: LedgeredArticleAnalysisRunner,
        live_window_days: int = 30,
        sentiment_window_days: int = 30,
        sentiment_half_life_hours: float = 24.0,
        overlap: timedelta = DEFAULT_OVERLAP,
        role_runner: LedgeredCompanyRoleRunner | None = None,
    ) -> None:
        if live_window_days < 1:
            raise ValueError("live_window_days must be positive")
        names = [source.name for source in sources]
        if len(names) != len(set(names)):
            raise ValueError("watermarked source names must be unique")
        self.constituents = constituents
        self.sources = tuple(sources)
        self.sentiment = sentiment
        self.repository = repository
        self.runner = runner
        self.live_window_days = live_window_days
        self.sentiment_window_days = sentiment_window_days
        self.sentiment_half_life_hours = sentiment_half_life_hours
        self.overlap = overlap
        self.role_runner = role_runner

    @property
    def contract(self) -> str:
        return self.runner.compatibility.contract_key

    def activate(self, symbol: str, *, now: datetime) -> CoverageReport:
        """Register a ticker and give every stored article a ledger state. Spends nothing.

        No fetch and no analysis call: stored current-contract analyses become ``analyzed``,
        history older than the live window becomes ``baseline``, per-article irrelevance becomes
        ``skipped``, and the remaining recent articles become ``pending``.
        """

        constituent = self.constituents.resolve(symbol)
        coverage = self.repository.activate_company_coverage(
            constituent.symbol, started_at=now, live_window_days=self.live_window_days
        )
        reconcile = self._reconcile(constituent, coverage, now)
        return self._report("activation", constituent, coverage, reconcile=reconcile)

    def status(self, symbol: str) -> CoverageReport:
        constituent = self.constituents.resolve(symbol)
        return self._report("status", constituent, self._active_coverage(constituent))

    def run(
        self,
        symbol: str,
        *,
        now: datetime,
        max_new_analyses: int,
        ingest: bool = True,
        analyze: bool = True,
        role_budget: RoleBudget | None = None,
        role_tickers: Iterable[str] | None = None,
    ) -> CoverageReport:
        if max_new_analyses < 0:
            raise ValueError("max_new_analyses must not be negative")
        listed = _require_role_tickers(role_budget, normalize_role_tickers(role_tickers))
        constituent = self.constituents.resolve(symbol)
        coverage = self._active_coverage(constituent)
        ingest_report = self._ingest(constituent, now) if ingest else IngestReport()
        reconcile = self._reconcile(constituent, coverage, now)
        analysis = (
            self._analyze(constituent, now, max_new_analyses) if analyze else AnalysisPassReport()
        )
        roles = (
            self._label_roles_many([(constituent, coverage)], now, role_budget, listed).get(
                constituent.symbol
            )
            if analyze
            else None
        )
        evidence = self._check_evidence(constituent, coverage, now)
        return self._report(
            "cycle",
            constituent,
            coverage,
            ingest=ingest_report,
            reconcile=reconcile,
            analysis=analysis,
            evidence=evidence,
            roles=roles,
        )

    def run_all(
        self,
        symbols: Sequence[str],
        *,
        now: datetime,
        max_new_per_ticker: int,
        max_new_total: int | None = None,
        ingest: bool = True,
        analyze: bool = True,
        role_budget: RoleBudget | None = None,
        role_tickers: Iterable[str] | None = None,
    ) -> list[CoverageRunResult]:
        """Cycle many tickers in one pass, allocating a shared ``max_new_total`` fairly.

        Ingest and reconcile run per ticker, in the given order, exactly as ``run`` would for
        each on its own. The paid analysis pass is then round-robin across every ticker that
        resolved and is under active coverage (see ``_analyze_many``): every ticker gets a turn
        before any ticker gets a second one, so a shared budget can no longer be exhausted by
        whichever tickers happen to sort first, leaving the rest of the batch with nothing. With
        exactly one symbol this reduces to exactly what ``run`` does -- there is no one else to
        take a turn, so nothing about a single-ticker call changes.

        A ticker that does not resolve, or is not under active coverage, is reported with an
        error rather than raised, so one bad ticker in a batch never discards the results already
        computed for the rest of it.
        """

        if max_new_per_ticker < 0:
            raise ValueError("max_new_per_ticker must not be negative")
        if max_new_total is not None and max_new_total < 0:
            raise ValueError("max_new_total must not be negative")
        listed = _require_role_tickers(role_budget, normalize_role_tickers(role_tickers))

        resolved: list[tuple[str, Constituent, CompanyCoverage]] = []
        outcomes: dict[str, CoverageRunResult] = {}
        for symbol in symbols:
            try:
                constituent = self.constituents.resolve(symbol)
                coverage = self._active_coverage(constituent)
            except (ConstituentNotFoundError, CoverageNotActiveError) as error:
                outcomes[symbol] = CoverageRunResult(ticker=symbol, error=str(error))
                continue
            resolved.append((symbol, constituent, coverage))

        ingest_reports = {
            symbol: self._ingest(constituent, now) if ingest else IngestReport()
            for symbol, constituent, _ in resolved
        }
        reconciles = {
            symbol: self._reconcile(constituent, coverage, now)
            for symbol, constituent, coverage in resolved
        }
        analyses_by_ticker: dict[str, AnalysisPassReport] = {}
        if analyze and resolved:
            analyses_by_ticker = self._analyze_many(
                [constituent for _, constituent, _ in resolved],
                now,
                max_new_per_ticker=max_new_per_ticker,
                max_new_total=max_new_total,
            )
        roles_by_ticker: dict[str, RolePassReport] = {}
        if analyze and resolved:
            roles_by_ticker = self._label_roles_many(
                [(constituent, coverage) for _, constituent, coverage in resolved],
                now,
                role_budget,
                listed,
            )

        for symbol, constituent, coverage in resolved:
            evidence = self._check_evidence(constituent, coverage, now)
            outcomes[symbol] = CoverageRunResult(
                ticker=symbol,
                report=self._report(
                    "cycle",
                    constituent,
                    coverage,
                    ingest=ingest_reports[symbol],
                    reconcile=reconciles[symbol],
                    analysis=analyses_by_ticker.get(constituent.symbol, AnalysisPassReport()),
                    evidence=evidence,
                    roles=roles_by_ticker.get(constituent.symbol),
                ),
            )
        return [outcomes[symbol] for symbol in symbols]

    def _active_coverage(self, constituent: Constituent) -> CompanyCoverage:
        coverage = self.repository.get_company_coverage(constituent.symbol)
        if coverage is None or not coverage.active:
            raise CoverageNotActiveError(
                f"{constituent.symbol} is not under continuous coverage; activate it first."
            )
        return coverage

    def _ingest(self, constituent: Constituent, now: datetime) -> IngestReport:
        attempts: list[tuple[WatermarkedNewsSource, datetime, str, bool, str | None, int, int]] = []
        fetched: list[Article] = []
        for source in self.sources:
            watermark = self.repository.get_ingestion_watermark(constituent.symbol, source.name)
            since, gap = fetch_window(
                watermark.ingested_through if watermark else None,
                now,
                overlap=self.overlap,
                max_lookback=source.max_lookback,
            )
            try:
                result = source.fetch(constituent, since, now)
            except Exception as exc:  # defensive: providers normally report health, never raise
                attempts.append((source, since, "failed", False, type(exc).__name__, 0, 0))
                continue
            status, completed, message = fetch_outcome(result)
            if gap:
                message = _join(
                    message,
                    "Watermark is older than this provider's lookback; the earlier span was not "
                    "re-covered.",
                )
            attempts.append(
                (
                    source,
                    since,
                    status,
                    completed,
                    message,
                    len(result.articles),
                    result.funnel.request_limited,
                )
            )
            fetched.extend(result.articles)

        combined = deduplicate_with_diagnostics(fetched)
        new_articles: list[Article] = []
        newly_scored = 0
        if combined.articles:
            earliest = min(article.published_at for article in combined.articles)
            stored = self.repository.list_articles(
                constituent.symbol, since=earliest - timedelta(days=1)
            )
            new_articles = [
                article
                for article in combined.articles
                if not any(deduplication_reason(article, item) for item in stored)
            ]
            self.repository.upsert_articles(new_articles)
            already_scored = self.repository.scored_fingerprints(
                article.fingerprint for article in new_articles
            )
            scored = self.sentiment.score(
                [article for article in new_articles if article.fingerprint not in already_scored]
            )
            self.repository.upsert_sentiments(scored)
            newly_scored = len(scored)
            if new_articles:
                self._recompute_daily_sentiment(constituent, now)

        # Watermarks move only after the fetched articles are durably stored.
        reports = []
        for source, since, status, completed, message, count, request_limited in attempts:
            self.repository.record_ingestion_attempt(
                ticker=constituent.symbol,
                provider=source.name,
                window_start=since,
                attempted_at=now,
                status=status,
                message=message,
                articles=count,
                request_limited=request_limited,
                ingested_through=now if completed else None,
            )
            reports.append(SourceReport(source.name, since, now, status, count, message))
        return IngestReport(
            sources=tuple(reports),
            fetched_unique=len(combined.articles),
            new_articles=len(new_articles),
            newly_scored=newly_scored,
        )

    def _recompute_daily_sentiment(self, constituent: Constituent, now: datetime) -> None:
        """The same aggregate -> delete-window -> upsert triad the live refresh performs."""

        cutoff = now - timedelta(days=self.sentiment_window_days)
        real_articles = [
            article
            for article in self.repository.list_scored_articles(constituent.symbol, since=cutoff)
            if not article.is_demo
        ]
        daily = aggregate_daily_sentiment(
            constituent.symbol, real_articles, half_life_hours=self.sentiment_half_life_hours
        )
        self.repository.delete_daily_sentiment(constituent.symbol, cutoff.date())
        self.repository.upsert_daily_sentiment(daily)

    def _reconcile(
        self, constituent: Constituent, coverage: CompanyCoverage, now: datetime
    ) -> ReconcileReport:
        missing = self.repository.articles_without_analysis_job(constituent.symbol, self.contract)
        if not missing:
            return ReconcileReport()
        analysed = self.repository.contract_analyses(constituent.symbol, self.runner.compatibility)
        horizon = coverage.ledger_started_at - timedelta(days=coverage.live_window_days)
        jobs: list[NewAnalysisJob] = []
        for article in missing:
            existing = analysed.get(article.fingerprint)
            if existing is not None:
                state, reason = "analyzed", "preexisting"
            elif article.is_demo:
                state, reason = "skipped", "demo"
            elif article.published_at < horizon:
                state, reason = "baseline", "pre_ledger_history"
            elif (skip := deterministic_skip_reason(article, constituent)) is not None:
                state, reason = "skipped", skip
            else:
                state, reason = "pending", "eligible"
            jobs.append(
                NewAnalysisJob(
                    article_fingerprint=article.fingerprint,
                    analysis_contract=self.contract,
                    ticker=constituent.symbol,
                    published_at=article.published_at,
                    state=state,
                    reason=reason,
                    created_at=now,
                    analysis_evidence_fingerprint=existing.evidence_fingerprint
                    if existing
                    else None,
                    analysis_created_at=existing.analysis_created_at if existing else None,
                )
            )
        self.repository.insert_analysis_jobs(jobs)
        return ReconcileReport(created=dict(Counter(job.state for job in jobs)))

    def _analyze(
        self, constituent: Constituent, now: datetime, max_new_analyses: int
    ) -> AnalysisPassReport:
        due = self.repository.list_due_analysis_jobs(constituent.symbol, self.contract, now)
        if not due:
            return AnalysisPassReport()
        due_ids = {job.article_fingerprint for job in due}
        articles = [
            article
            for article in self.repository.list_articles(constituent.symbol)
            if article.fingerprint in due_ids
        ]
        ordered = processing_order(articles, now, constituent)

        counts: Counter[str] = Counter()
        consecutive_failures = 0
        stop_reason: str | None = None
        deferred = 0
        for index, article in enumerate(ordered):
            if counts["paid"] >= max_new_analyses:
                stop_reason, deferred = "budget", len(ordered) - index
                break
            # The runner uses its own clock, so a lease taken late in a long run is still fresh.
            outcome = self.runner.process(article.fingerprint)
            if outcome.stops_run:
                stop_reason, deferred = "provider_unavailable", len(ordered) - index
                break
            if outcome.reused or outcome.response.status == "cached":
                counts["reused"] += 1
                consecutive_failures = 0
                continue
            if outcome.refused:
                counts["refused"] += 1
                continue
            if outcome.paid_attempt:
                counts["paid"] += 1
            if outcome.response.status == "generated":
                counts["generated"] += 1
                consecutive_failures = 0
                continue
            if outcome.job_state == "skipped":
                counts["skipped"] += 1
                continue
            counts["retry_scheduled" if outcome.job_state == "retry_wait" else "failed"] += 1
            if outcome.paid_attempt:
                consecutive_failures += 1
            if consecutive_failures >= 2:
                stop_reason, deferred = "circuit_breaker", len(ordered) - index - 1
                break

        return AnalysisPassReport(
            claimable=len(ordered),
            paid_attempts=counts["paid"],
            generated=counts["generated"],
            reused=counts["reused"],
            retry_scheduled=counts["retry_scheduled"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            refused=counts["refused"],
            deferred=deferred,
            stop_reason=stop_reason,
        )

    def _analyze_many(
        self,
        constituents: Sequence[Constituent],
        now: datetime,
        *,
        max_new_per_ticker: int,
        max_new_total: int | None,
    ) -> dict[str, AnalysisPassReport]:
        """Fair, deterministic paid-analysis allocation across several tickers in one pass.

        Round-robin by ticker, in the given order: each ticker's turn silently advances past
        every *free* outcome in its own due-article queue (a reused/cached analysis, a refusal, a
        deterministic skip -- anything with ``paid_attempt`` false) and stops the instant it
        makes one genuine paid attempt, so every other still-eligible ticker gets its own paid
        attempt before this one gets a second. A ticker leaves the rotation once its queue is
        drained, its own ``max_new_per_ticker`` cap is reached, or two of its own consecutive paid
        attempts failed -- the same breaker ``_analyze`` applies to a single ticker, now scoped
        per ticker so one ticker tripping it never affects another's turn. The whole pass stops
        only when ``max_new_total`` is exhausted or the provider reports itself unavailable
        (nothing left that anyone could still pay for); every ticker still holding queued work at
        that point is reported with it in ``deferred`` and a matching ``stop_reason``, exactly as
        a single-ticker budget cutoff already reports it -- nothing is dropped, only left queued
        for a later run.

        With exactly one ticker in ``constituents``, there is no one else to take a turn: this
        reduces to processing that ticker's whole queue in order, identically to ``_analyze``.
        """

        queues: dict[str, _TickerQueue] = {}
        order: list[str] = []
        for constituent in constituents:
            due = self.repository.list_due_analysis_jobs(constituent.symbol, self.contract, now)
            ordered: list[Article] = []
            if due:
                due_ids = {job.article_fingerprint for job in due}
                articles = [
                    article
                    for article in self.repository.list_articles(constituent.symbol)
                    if article.fingerprint in due_ids
                ]
                ordered = processing_order(articles, now, constituent)
            queues[constituent.symbol] = _TickerQueue(ordered=ordered)
            order.append(constituent.symbol)

        remaining_total = max_new_total
        provider_unavailable = False
        active = [ticker for ticker in order if queues[ticker].ordered]

        while (
            active and not provider_unavailable and (remaining_total is None or remaining_total > 0)
        ):
            still_active: list[str] = []
            for ticker in active:
                if remaining_total is not None and remaining_total <= 0:
                    break  # global budget exhausted mid-round: no one else gets a turn either
                queue = queues[ticker]
                while queue.position < len(queue.ordered):
                    article = queue.ordered[queue.position]
                    queue.position += 1
                    # The runner uses its own clock, so a lease taken late in a long run is fresh.
                    outcome = self.runner.process(article.fingerprint)
                    if outcome.stops_run:
                        # Nothing was spent and the job stayed pending: this article was not
                        # actually consumed, so undo the optimistic advance and let it still
                        # count as deferred, exactly as a single-ticker budget cutoff would.
                        queue.position -= 1
                        provider_unavailable = True
                        break
                    if not outcome.paid_attempt:
                        if outcome.reused or outcome.response.status == "cached":
                            queue.counts["reused"] += 1
                            queue.consecutive_failures = 0
                        elif outcome.refused:
                            queue.counts["refused"] += 1
                        elif outcome.job_state == "skipped":
                            queue.counts["skipped"] += 1
                        else:
                            key = (
                                "retry_scheduled" if outcome.job_state == "retry_wait" else "failed"
                            )
                            queue.counts[key] += 1
                        continue  # free: keep going within this same turn
                    queue.paid += 1
                    if remaining_total is not None:
                        remaining_total -= 1
                    if outcome.response.status == "generated":
                        queue.counts["generated"] += 1
                        queue.consecutive_failures = 0
                    else:
                        key = "retry_scheduled" if outcome.job_state == "retry_wait" else "failed"
                        queue.counts[key] += 1
                        queue.consecutive_failures += 1
                        if queue.consecutive_failures >= 2:
                            queue.stop_reason = "circuit_breaker"
                    break  # a paid attempt always yields the turn to the next ticker
                if provider_unavailable:
                    break
                if (
                    queue.stop_reason is None
                    and queue.paid < max_new_per_ticker
                    and queue.position < len(queue.ordered)
                ):
                    still_active.append(ticker)
            active = still_active

        reports: dict[str, AnalysisPassReport] = {}
        for ticker in order:
            queue = queues[ticker]
            deferred = len(queue.ordered) - queue.position
            stop_reason = queue.stop_reason
            if deferred > 0 and stop_reason is None:
                stop_reason = "provider_unavailable" if provider_unavailable else "budget"
            reports[ticker] = AnalysisPassReport(
                claimable=len(queue.ordered),
                paid_attempts=queue.paid,
                generated=queue.counts["generated"],
                reused=queue.counts["reused"],
                retry_scheduled=queue.counts["retry_scheduled"],
                failed=queue.counts["failed"],
                skipped=queue.counts["skipped"],
                refused=queue.counts["refused"],
                deferred=deferred,
                stop_reason=stop_reason,
            )
        return reports

    def _label_roles_many(
        self,
        targets: Sequence[tuple[Constituent, CompanyCoverage]],
        now: datetime,
        budget: RoleBudget | None,
        role_tickers: frozenset[str] | None = None,
    ) -> dict[str, RolePassReport]:
        """The company-role pass: reconcile, then label under explicit caps, deterministically.

        Only tickers in ``role_tickers`` are labelled. A covered ticker not on the list is dropped
        before reconcile: no job, no paid call, no share of any cap. A caller reaching this with
        an enabled budget and no list is refused.

        Inert unless a role runner is wired *and* a cap is positive: with the delivered zero
        defaults it creates no job and makes no call. Each article is paid for once per role
        contract (the ledger lease and the stored-label check), budget-limited work stays
        ``pending``, and articles in sessions that can change an `mr-v1` result come first.

        Two phases, each round-robin across tickers so a shared cap cannot be exhausted by
        whichever ticker sorts first: *new* articles (published inside the live window) under
        ``max_new_per_ticker`` / ``max_new_total``, then *backfill* (older stored articles) under
        ``max_backfill_total``. Two consecutive paid failures stop a ticker, and an unavailable
        provider stops everything without consuming an attempt.
        """

        runner = self.role_runner
        if runner is None or budget is None or not budget.enabled:
            return {}
        listed = _require_role_tickers(budget, role_tickers) or frozenset()

        contract = runner.contract_key
        queues: dict[str, _RoleQueue] = {}
        order: list[str] = []
        for constituent, coverage in targets:
            if constituent.symbol.upper() not in listed:
                continue
            reconciled = reconcile_role_jobs(self.repository, contract, constituent.symbol, now)
            due = self.repository.list_due_analysis_jobs(constituent.symbol, contract, now)
            due_ids = {job.article_fingerprint for job in due}
            articles = [
                article
                for article in self.repository.list_articles(constituent.symbol)
                if article.fingerprint in due_ids
            ]
            priority = priority_article_ids(
                self.repository.list_scored_articles(constituent.symbol, limit=None),
                constituent.market,
            )
            ordered = role_processing_order(articles, priority)
            queues[constituent.symbol] = _RoleQueue(
                reconciled=reconciled,
                new=[a for a in ordered if is_new_article(a, now, coverage.live_window_days)],
                backfill=[
                    a for a in ordered if not is_new_article(a, now, coverage.live_window_days)
                ],
            )
            order.append(constituent.symbol)

        provider_unavailable = False

        def drain(phase: str, total_cap: int, per_ticker_cap: int | None) -> None:
            nonlocal provider_unavailable
            remaining = total_cap
            active = [t for t in order if queues[t].items(phase)]
            while active and not provider_unavailable and remaining > 0:
                still_active: list[str] = []
                for ticker in active:
                    if remaining <= 0:
                        break
                    queue = queues[ticker]
                    items = queue.items(phase)
                    while queue.stop_reason is None and queue.positions[phase] < len(items):
                        article = items[queue.positions[phase]]
                        queue.positions[phase] += 1
                        outcome = runner.process(article.fingerprint)
                        if outcome.stops_run:
                            # Nothing was spent and the job stayed pending: not consumed.
                            queue.positions[phase] -= 1
                            provider_unavailable = True
                            break
                        if not outcome.paid_attempt:
                            if outcome.reused or outcome.response.status == "cached":
                                queue.counts["reused"] += 1
                                queue.consecutive_failures = 0
                            elif outcome.refused:
                                queue.counts["refused"] += 1
                            elif outcome.job_state == "skipped":
                                queue.counts["skipped"] += 1
                            else:
                                queue.counts["failed"] += 1
                            continue  # free: keep going within this same turn
                        queue.paid[phase] += 1
                        remaining -= 1
                        if outcome.response.status == "generated":
                            queue.counts["generated"] += 1
                            queue.consecutive_failures = 0
                        else:
                            key = (
                                "retry_scheduled" if outcome.job_state == "retry_wait" else "failed"
                            )
                            queue.counts[key] += 1
                            queue.consecutive_failures += 1
                            if queue.consecutive_failures >= 2:
                                queue.stop_reason = "circuit_breaker"
                        break  # a paid attempt always yields the turn to the next ticker
                    if provider_unavailable:
                        break
                    if (
                        queue.stop_reason is None
                        and queue.positions[phase] < len(items)
                        and (per_ticker_cap is None or queue.paid[phase] < per_ticker_cap)
                    ):
                        still_active.append(ticker)
                active = still_active

        if budget.max_new_per_ticker > 0 and budget.max_new_total > 0:
            drain("new", budget.max_new_total, budget.max_new_per_ticker)
        if budget.max_backfill_total > 0:
            drain("backfill", budget.max_backfill_total, None)

        reports: dict[str, RolePassReport] = {}
        for ticker in order:
            queue = queues[ticker]
            deferred_new = len(queue.new) - queue.positions["new"]
            deferred_backfill = len(queue.backfill) - queue.positions["backfill"]
            stop_reason = queue.stop_reason
            if stop_reason is None and (deferred_new or deferred_backfill):
                stop_reason = "provider_unavailable" if provider_unavailable else "budget"
            ledger = self.repository.analysis_job_summary(ticker, contract)
            reports[ticker] = RolePassReport(
                reconciled=queue.reconciled,
                claimable_new=len(queue.new),
                claimable_backfill=len(queue.backfill),
                paid_attempts=sum(queue.paid.values()),
                generated=queue.counts["generated"],
                reused=queue.counts["reused"],
                retry_scheduled=queue.counts["retry_scheduled"],
                failed=queue.counts["failed"],
                skipped=queue.counts["skipped"],
                refused=queue.counts["refused"],
                deferred_new=deferred_new,
                deferred_backfill=deferred_backfill,
                stop_reason=stop_reason,
                ledger_states=ledger.states,
                ledger_input_tokens=ledger.input_tokens,
                ledger_output_tokens=ledger.output_tokens,
            )
        return reports

    def _check_evidence(
        self, constituent: Constituent, coverage: CompanyCoverage, now: datetime
    ) -> EvidenceCheckReport:
        """Record whether each analysed job's evidence is still what a fresh analysis would see.

        Checks analyses never checked before, plus any whose article is recent enough that its
        evidence pool can still be growing. Free: no provider call and no regeneration.
        """

        window_after = timedelta(days=EVIDENCE_WINDOW_DAYS_AFTER)
        recent_cutoff = now - timedelta(days=coverage.live_window_days) - window_after
        jobs = [
            job
            for job in self.repository.list_analysis_jobs(
                constituent.symbol, self.contract, states=("analyzed",)
            )
            if job.evidence_checked_at is None or job.published_at >= recent_cutoff
        ]
        if not jobs:
            return EvidenceCheckReport()
        newest = self.repository.contract_analyses(constituent.symbol, self.runner.compatibility)
        checked = changed = window_open = 0
        for job in jobs:
            analysis = newest.get(job.article_fingerprint)
            if analysis is None:
                continue
            current = self.runner.analysis_service.current_evidence_fingerprint(
                job.article_fingerprint
            )
            if current is None:
                continue
            is_current = current == analysis.evidence_fingerprint
            self.repository.record_evidence_check(
                job.article_fingerprint,
                self.contract,
                analysis=analysis,
                current=is_current,
                now=now,
            )
            checked += 1
            changed += 0 if is_current else 1
            window_open += 1 if now < job.published_at + window_after else 0
        return EvidenceCheckReport(checked=checked, changed=changed, window_open=window_open)

    def _report(
        self,
        mode: str,
        constituent: Constituent,
        coverage: CompanyCoverage,
        *,
        ingest: IngestReport | None = None,
        reconcile: ReconcileReport | None = None,
        analysis: AnalysisPassReport | None = None,
        evidence: EvidenceCheckReport | None = None,
        roles: RolePassReport | None = None,
    ) -> CoverageReport:
        return CoverageReport(
            ticker=constituent.symbol,
            mode=mode,
            analysis_contract=self.contract,
            coverage=coverage,
            ingest=ingest or IngestReport(),
            reconcile=reconcile or ReconcileReport(),
            analysis=analysis or AnalysisPassReport(),
            evidence=evidence or EvidenceCheckReport(),
            summary=self.repository.analysis_job_summary(constituent.symbol, self.contract),
            watermarks=tuple(self.repository.list_ingestion_watermarks(constituent.symbol)),
            roles=roles,
        )


def _join(existing: str | None, addition: str) -> str:
    return f"{existing} {addition}" if existing else addition
