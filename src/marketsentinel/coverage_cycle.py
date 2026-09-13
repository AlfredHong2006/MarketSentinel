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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.analysis_ledger import (
    LedgeredArticleAnalysisRunner,
    deterministic_skip_reason,
    processing_order,
)
from marketsentinel.domain import Article, Constituent, NewsFetchResult
from marketsentinel.errors import CoverageNotActiveError
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
    """A recent-news provider such as Google News RSS, which always reads up to now."""

    name: str
    provider: NewsProvider
    max_lookback: timedelta
    max_articles: int

    def fetch(self, constituent: Constituent, since: datetime, until: datetime) -> NewsFetchResult:
        del until
        return self.provider.fetch(constituent, since, self.max_articles)


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
class EvidenceCheckReport:
    checked: int = 0
    changed: int = 0
    window_open: int = 0


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
    ) -> CoverageReport:
        if max_new_analyses < 0:
            raise ValueError("max_new_analyses must not be negative")
        constituent = self.constituents.resolve(symbol)
        coverage = self._active_coverage(constituent)
        ingest_report = self._ingest(constituent, now) if ingest else IngestReport()
        reconcile = self._reconcile(constituent, coverage, now)
        analysis = (
            self._analyze(constituent, now, max_new_analyses) if analyze else AnalysisPassReport()
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
        )

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
        )


def _join(existing: str | None, addition: str) -> str:
    return f"{existing} {addition}" if existing else addition
