"""Manually-triggered, synchronous, one-shot historical intelligence backfill.

Reuses the live analysis pipeline's own building blocks -- candidate selection
(``analysis_candidates.select_analysis_candidates_with_diagnostics``), the article-analysis
runner protocol (``service.ArticleAnalysisRunner``), and sentiment aggregation
(``aggregation.sentiment.aggregate_daily_sentiment``) -- over disjoint calendar-month buckets,
bounded by an explicit run-level budget. This never touches the live ``/analyze`` funnel, its
caps, or ``analysis_compatibility.py``'s exact-equality display rule; it only produces more
data for that unchanged machinery to read.

No scheduler, queue, or background worker: this class is driven by one manual script invocation
and returns when the bounded work is done.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.analysis_candidates import select_analysis_candidates_with_diagnostics
from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.domain import Article, Constituent, DailySentiment, ScoredArticle
from marketsentinel.historical_backfill import (
    BackfillBucket,
    BackfillBucketReport,
    BackfillPlanReport,
    BackfillRefusal,
    BackfillRunReport,
    EvidenceRefreshReport,
    SelectionGapBucketReport,
    SelectionGapReport,
    StaleBacklogReport,
    plan_backfill_buckets,
    resolve_backfill_boundary,
)
from marketsentinel.sentiment.finbert import SentimentAnalyzer
from marketsentinel.service import ArticleAnalysisRunner
from marketsentinel.sources.historical import HistoricalNewsProvider, HistoricalNewsService
from marketsentinel.storage.sqlite import SQLiteRepository
from marketsentinel.timeutils import ensure_utc

# list_scored_articles/list_article_analyses default to smaller limits sized for one live
# request's ~30-day window; a full-horizon backfill query needs generous, explicit caps of its
# own so a large stored corpus is never silently truncated (the same class of fix as service.py's
# _STORED_ANALYSES_LIMIT).
_SENTIMENT_AGGREGATION_LIMIT = 5_000
_STALE_BACKLOG_QUERY_LIMIT = 5_000


class ScoredReadCapExceeded(RuntimeError):
    """A scored-article read hit its cap mid-run, so its result is truncated and was not used.

    Raised instead of returning the rows: a truncated read silently drops the OLDEST articles
    (newest first), which is exactly the range a deep backfill adds.
    """

    def __init__(self, *, cap: int, phase: str, state: str) -> None:
        self.cap = cap
        self.phase = phase
        self.state = state
        super().__init__(
            f"More than {cap} scored articles are stored in the range read {phase}, so the read "
            f"is truncated and was not used. {state}"
        )


def stored_history_start(repository: SQLiteRepository, ticker: str) -> datetime | None:
    """The earliest ``published_at`` among the ticker's non-demo stored articles, if any."""

    return min(
        (
            ensure_utc(article.published_at)
            for article in repository.list_articles(ticker, since=None)
            if not article.is_demo
        ),
        default=None,
    )


def resolve_anchored_boundary(
    repository: SQLiteRepository,
    symbol: str,
    *,
    now: datetime,
    horizon_days: int,
    override: datetime | None = None,
) -> datetime:
    """The exclusive end for a boundary-anchored run, read from the database the run uses.

    Reads only: no constituent lookup, no network, no write, so it can run on a read-only
    repository. Raises ``BackfillRefusal`` when the ticker has no stored articles or the boundary
    is outside the horizon.
    """

    ticker = symbol.strip().upper()
    return resolve_backfill_boundary(
        ticker=ticker,
        stored_start=stored_history_start(repository, ticker),
        override=override,
        now=now,
        horizon_days=horizon_days,
    )


def plan_anchored_backfill(
    repository: SQLiteRepository,
    symbol: str,
    *,
    now: datetime,
    horizon_days: int,
    override: datetime | None = None,
    read_cap: int = _SENTIMENT_AGGREGATION_LIMIT,
) -> BackfillPlanReport:
    """Resolve and describe a boundary-anchored run. Reads only; fetches and writes nothing."""

    ticker = symbol.strip().upper()
    boundary = resolve_anchored_boundary(
        repository, ticker, now=now, horizon_days=horizon_days, override=override
    )
    stored = sorted(
        ensure_utc(article.published_at)
        for article in repository.list_articles(ticker, since=None)
        if not article.is_demo
    )
    scored = repository.list_scored_articles(
        ticker, since=now - timedelta(days=horizon_days), limit=read_cap + 1
    )
    return BackfillPlanReport(
        ticker=ticker,
        now=now,
        horizon_days=horizon_days,
        boundary=boundary,
        boundary_source="explicit --until" if override is not None else "earliest stored",
        stored_start=stored[0],
        earliest_published=tuple(stored[:5]),
        stored_total=len(stored),
        stored_in_30_days_after_boundary=sum(
            1 for value in stored if boundary <= value < boundary + timedelta(days=30)
        ),
        scored_in_horizon=len(scored),
        read_cap=read_cap,
        buckets=tuple(plan_backfill_buckets(now, horizon_days=horizon_days, until=boundary)),
    )


class ConstituentResolver(Protocol):
    def resolve(self, symbol: str) -> Constituent: ...


class HistoricalIntelligenceBackfillService:
    def __init__(
        self,
        constituents: ConstituentResolver,
        historical_news: HistoricalNewsProvider | HistoricalNewsService,
        sentiment: SentimentAnalyzer,
        repository: SQLiteRepository,
        article_analysis_runner: ArticleAnalysisRunner,
        article_analysis_compatibility: ArticleAnalysisCompatibility,
        *,
        bucket_candidate_cap: int = 5,
        bucket_max_articles: int = 200,
        max_new_analyses_per_run: int = 60,
        sentiment_half_life_hours: float = 24.0,
        priority_bonus_limit: int = 0,
        scored_read_cap: int = _SENTIMENT_AGGREGATION_LIMIT,
    ) -> None:
        if scored_read_cap < 1:
            raise ValueError("scored_read_cap must be positive")
        if not 1 <= bucket_candidate_cap <= 40:
            raise ValueError("bucket_candidate_cap must be between 1 and 40")
        if max_new_analyses_per_run < 0:
            raise ValueError("max_new_analyses_per_run must not be negative")
        if not 0 <= priority_bonus_limit <= 10:
            raise ValueError("priority_bonus_limit must be between 0 and 10")
        self.constituents = constituents
        self.historical_news = historical_news
        self.sentiment = sentiment
        self.repository = repository
        self.article_analysis_runner = article_analysis_runner
        self.article_analysis_compatibility = article_analysis_compatibility
        self.bucket_candidate_cap = bucket_candidate_cap
        self.bucket_max_articles = bucket_max_articles
        self.max_new_analyses_per_run = max_new_analyses_per_run
        self.sentiment_half_life_hours = sentiment_half_life_hours
        self.priority_bonus_limit = priority_bonus_limit
        self.scored_read_cap = scored_read_cap

    def _read_scored(
        self, ticker: str, since: datetime, *, phase: str, state: str
    ) -> list[ScoredArticle]:
        """List scored articles, refusing a result that hit the cap.

        One row more than the cap is requested, so exactly ``scored_read_cap`` rows is a complete
        read and anything beyond it is detected rather than silently cut off.
        """

        rows = self.repository.list_scored_articles(
            ticker, since=since, limit=self.scored_read_cap + 1
        )
        if len(rows) > self.scored_read_cap:
            raise ScoredReadCapExceeded(cap=self.scored_read_cap, phase=phase, state=state)
        return rows

    def _refuse_if_scored_reads_would_truncate(self, ticker: str, since: datetime) -> None:
        """Before any fetch: a corpus that already fills the cap cannot be read back completely."""

        rows = self.repository.list_scored_articles(ticker, since=since, limit=self.scored_read_cap)
        if len(rows) >= self.scored_read_cap:
            raise BackfillRefusal(
                f"{ticker} already has {self.scored_read_cap} or more scored articles stored since "
                f"{since.isoformat()}, which is the read cap ({self.scored_read_cap}). The run "
                "reads them back newest first to rebuild daily sentiment, so storing more would "
                "silently drop the oldest. Nothing was fetched or written. Raising the cap is a "
                "decision for the product owner (see docs/research/MR-009-proposal.md)."
            )

    def backfill(
        self,
        symbol: str,
        *,
        now: datetime,
        horizon_days: int = 366,
        offset_days: int = 0,
        until: datetime | None = None,
    ) -> BackfillRunReport:
        """Populate historical articles/sentiment/analyses for one ticker. Idempotent to re-run.

        ``offset_days`` skips the most recent days of the horizon (see ``plan_backfill_buckets``):
        nothing is fetched for them. The daily-sentiment rebuild below still spans everything
        stored from the horizon start onward, as it always has, so the skipped range is rebuilt
        from its stored articles rather than left stale.

        ``until`` ends the run exactly at an instant (the start of stored history): the bucket
        containing it is clipped there and no article published at or after it is stored, whatever
        the provider returns. Mutually exclusive with ``offset_days``.

        A scored-article read that reaches ``scored_read_cap`` is never used as if complete: the
        run refuses before any fetch when the stored corpus already fills the cap, and raises
        ``ScoredReadCapExceeded`` (before any daily-sentiment rewrite) if storing pushes it past.

        Two phases, not one pass per bucket: every bucket's articles are fetched, persisted, and
        scored FIRST; only then does candidate selection/analysis begin. analyze_article's own
        cache key includes an evidence_fingerprint over same-ticker comparison articles already
        in storage at call time -- if analysis started before later buckets were persisted, a
        first run would see a smaller evidence pool than a second run does, changing the
        fingerprint and busting the cache even though nothing genuinely changed. Fetching
        everything before analyzing anything keeps that pool identical run over run.
        """

        run_start = now - timedelta(days=horizon_days)
        # Everything that can refuse the run happens before the first constituent lookup or fetch.
        buckets = plan_backfill_buckets(
            now, horizon_days=horizon_days, offset_days=offset_days, until=until
        )
        self._refuse_if_scored_reads_would_truncate(symbol.strip().upper(), run_start)
        constituent = self.constituents.resolve(symbol)

        fetch_outcomes = [
            self._fetch_and_score_bucket(constituent, bucket, exclusive_end=until)
            for bucket in buckets
        ]

        budget = _AnalysisBudget(self.max_new_analyses_per_run)
        bucket_reports = [
            self._select_and_analyze_bucket(constituent, bucket, outcome, budget)
            for bucket, outcome in zip(buckets, fetch_outcomes, strict=True)
        ]

        daily = self._recompute_historical_sentiment(constituent, run_start)
        bucket_reports = [
            replace(
                report,
                sentiment_dates_produced=_count_dates_in_bucket(daily, report.bucket),
            )
            for report in bucket_reports
        ]

        return BackfillRunReport(
            ticker=constituent.symbol,
            horizon_days=horizon_days,
            buckets=tuple(bucket_reports),
            new_analyses_attempted=budget.attempts,
            circuit_breaker_tripped=budget.tripped,
            sentiment_dates_total=len(daily),
            offset_days=offset_days,
            until=until,
        )

    def fill_selection_gaps(
        self, symbol: str, *, now: datetime, horizon_days: int
    ) -> SelectionGapReport:
        """Re-select candidates from already-stored articles and analyze only the gaps.

        This repairs a corpus whose candidate selection was wrong when it was first built, and it
        deliberately does none of ``backfill``'s other work: no historical-news fetch, no article
        upsert, no FinBERT scoring, no daily-sentiment rebuild. That is what makes it safe to
        re-run against a paid corpus -- leaving stored articles untouched keeps every analysis's
        evidence pool, and therefore its ``evidence_fingerprint``, exactly as it was, so
        already-analyzed reselections stay free cache hits instead of silently regenerating.

        ``horizon_days`` is required rather than defaulted: bucket boundaries follow from
        ``now`` and the horizon together, and a repair aimed at a specific historical run has to
        reproduce that run's geometry rather than inherit an unrelated default. Pinning ``now``
        freezes those boundaries and which publication dates fall inside them -- it does not
        freeze database membership, so an article stored later while carrying an old
        ``published_at`` still joins its bucket's pool. Verify the stored article count
        immediately before a repair rather than assuming the pinned timestamp isolates it.

        ``constituents`` must resolve offline (see ``CacheOnlyConstituentResolver``); nothing here
        can otherwise guarantee the run stays off the network.
        """

        constituent = self.constituents.resolve(symbol)
        buckets = plan_backfill_buckets(now, horizon_days=horizon_days)
        budget = _AnalysisBudget(self.max_new_analyses_per_run)
        reports = [self._fill_bucket_gaps(constituent, bucket, budget) for bucket in buckets]
        return SelectionGapReport(
            ticker=constituent.symbol,
            horizon_days=horizon_days,
            as_of=now,
            buckets=tuple(reports),
            new_analyses_attempted=budget.attempts,
            circuit_breaker_tripped=budget.tripped,
        )

    def _fill_bucket_gaps(
        self, constituent: Constituent, bucket: BackfillBucket, budget: "_AnalysisBudget"
    ) -> SelectionGapBucketReport:
        stored = [
            item
            for item in self._read_scored(
                constituent.symbol,
                bucket.start,
                phase=f"for bucket {bucket.label} (selection-gap fill)",
                state="Nothing was fetched, and no analysis was started for this bucket.",
            )
            if not item.is_demo and item.published_at < bucket.end
        ]
        selection = select_analysis_candidates_with_diagnostics(
            stored,
            bucket.end,
            self.bucket_candidate_cap,
            subject_company=constituent,
            prioritize_disclosures=True,
            priority_bonus_limit=self.priority_bonus_limit,
        )
        candidates = selection.candidates

        if budget.stopped:
            stop_reason = (
                "run-level circuit breaker already tripped"
                if budget.tripped
                else "run-level analysis budget already exhausted"
            )
            return SelectionGapBucketReport(
                bucket=bucket,
                stored_articles=len(stored),
                candidates_selected=len(candidates),
                message=f"Analysis skipped: {stop_reason}.",
            )

        message: str | None = None
        cache_hits = newly_analyzed = failed = 0
        for index, candidate in enumerate(candidates):
            if budget.stopped:
                remaining = len(candidates) - index
                message = (
                    f"{remaining} candidate(s) not analyzed: run budget/circuit breaker stopped."
                )
                break
            response = self.article_analysis_runner.analyze_article(candidate.fingerprint)
            if response.status == "cached":
                cache_hits += 1
                budget.record(response.status)
                continue
            if budget.record(response.status) == "completed":
                newly_analyzed += 1
            else:
                failed += 1

        return SelectionGapBucketReport(
            bucket=bucket,
            stored_articles=len(stored),
            candidates_selected=len(candidates),
            cache_hits=cache_hits,
            newly_analyzed=newly_analyzed,
            failed=failed,
            message=message,
        )

    def refresh_evidence(self, symbol: str, *, now: datetime) -> EvidenceRefreshReport:
        """Bounded, explicit maintenance pass for after an evidence-selection algorithm change
        (e.g. the temporal-window fix): re-runs analyze_article for every currently
        display-compatible analysis and lets its existing accepts_for_cache/evidence_fingerprint
        check decide the outcome -- unchanged evidence is a free cache hit, changed evidence is a
        normal bounded regeneration. No new compatibility semantics are introduced; this reuses
        the same cache machinery analyze_article already has. Conceptually separate from
        reanalyze_stale (M4), which targets version/schema incompatibility, not evidence-context
        staleness.
        """

        del now
        constituent = self.constituents.resolve(symbol)
        current = self.repository.list_article_analyses(
            constituent.symbol,
            since=None,
            limit=_STALE_BACKLOG_QUERY_LIMIT,
            compatibility=self.article_analysis_compatibility,
        )
        budget = _AnalysisBudget(self.max_new_analyses_per_run)
        cache_hits = regenerated = failed = 0
        for analysis in current:
            if budget.stopped:
                break
            response = self.article_analysis_runner.analyze_article(analysis.article_id)
            if response.status == "cached":
                cache_hits += 1
                budget.record(response.status)
                continue
            outcome = budget.record(response.status)
            if outcome == "completed":
                regenerated += 1
            else:
                failed += 1

        return EvidenceRefreshReport(
            ticker=constituent.symbol,
            candidates_checked=len(current),
            cache_hits=cache_hits,
            regenerated=regenerated,
            failed=failed,
            circuit_breaker_tripped=budget.tripped,
        )

    def reanalyze_stale(self, symbol: str, *, now: datetime) -> StaleBacklogReport:
        """Bounded, deliberate catch-up for articles whose only stored analyses predate the
        currently running Stage A/B/C version. ``accepts_for_display``'s exact-equality rule is
        never relaxed -- this only supplies fresh, current-version analyses for it to accept.
        """

        del now  # no date scoping: the backlog is defined purely by version incompatibility
        constituent = self.constituents.resolve(symbol)
        backlog = self._stale_analysis_backlog(constituent)
        budget = _AnalysisBudget(self.max_new_analyses_per_run)
        reanalyzed = failed = 0
        for article in backlog:
            if budget.stopped:
                break
            response = self.article_analysis_runner.analyze_article(article.fingerprint)
            outcome = budget.record(response.status)
            if outcome == "completed":
                reanalyzed += 1
            else:
                failed += 1

        return StaleBacklogReport(
            ticker=constituent.symbol,
            backlog_size=len(backlog),
            attempted=budget.attempts,
            reanalyzed=reanalyzed,
            failed=failed,
            circuit_breaker_tripped=budget.tripped,
        )

    def _fetch_and_score_bucket(
        self,
        constituent: Constituent,
        bucket: BackfillBucket,
        exclusive_end: datetime | None = None,
    ) -> "_BucketFetchOutcome":
        try:
            articles, fetch_status, health_message = self._fetch_bucket_articles(
                constituent, bucket
            )
        except Exception as exc:  # defensive: providers normally report health, never raise
            return _BucketFetchOutcome(fetch_status="failed", message=str(exc))

        if exclusive_end is not None:
            # Providers bound a window inclusively (published_at <= until); an anchored run stores
            # nothing at or after its boundary, so the instant itself is dropped here, before storage.
            articles = [item for item in articles if item.published_at < exclusive_end]

        if not articles:
            return _BucketFetchOutcome(fetch_status=fetch_status, message=health_message)

        self.repository.upsert_articles(articles)
        already_scored = self.repository.scored_fingerprints(
            article.fingerprint for article in articles
        )
        pending = [article for article in articles if article.fingerprint not in already_scored]
        newly_scored = self.sentiment.score(pending)
        self.repository.upsert_sentiments(newly_scored)

        bucket_scored = [
            item
            for item in self._read_scored(
                constituent.symbol,
                bucket.start,
                phase=f"after storing bucket {bucket.label}",
                state=(
                    f"Articles and scores for the buckets up to and including {bucket.label} are "
                    "stored in this database; later buckets were not fetched; daily_sentiment was "
                    "not rewritten and no analysis ran for this bucket."
                ),
            )
            if not item.is_demo and item.published_at < bucket.end
        ]
        return _BucketFetchOutcome(
            fetch_status=fetch_status,
            message=health_message,
            articles_fetched=len(articles),
            distinct_publishers=len({item.source for item in bucket_scored}),
            scored_articles=bucket_scored,
        )

    def _select_and_analyze_bucket(
        self,
        constituent: Constituent,
        bucket: BackfillBucket,
        fetch_outcome: "_BucketFetchOutcome",
        budget: "_AnalysisBudget",
    ) -> BackfillBucketReport:
        if fetch_outcome.articles_fetched == 0:
            return BackfillBucketReport(
                bucket=bucket,
                fetch_status=fetch_outcome.fetch_status,
                message=fetch_outcome.message,
            )

        selection = select_analysis_candidates_with_diagnostics(
            fetch_outcome.scored_articles,
            bucket.end,
            self.bucket_candidate_cap,
            subject_company=constituent,
            prioritize_disclosures=True,
            priority_bonus_limit=self.priority_bonus_limit,
        )
        candidates = selection.candidates
        message = fetch_outcome.message

        if budget.stopped:
            stop_reason = (
                "run-level circuit breaker already tripped"
                if budget.tripped
                else "run-level analysis budget already exhausted"
            )
            return BackfillBucketReport(
                bucket=bucket,
                fetch_status=fetch_outcome.fetch_status,
                articles_fetched=fetch_outcome.articles_fetched,
                distinct_publishers=fetch_outcome.distinct_publishers,
                candidates_selected=len(candidates),
                message=_join_messages(message, f"Analysis skipped: {stop_reason}."),
            )

        completed = failed = 0
        for index, candidate in enumerate(candidates):
            if budget.stopped:
                remaining = len(candidates) - index
                message = _join_messages(
                    message,
                    f"{remaining} candidate(s) not analyzed: run budget/circuit breaker stopped.",
                )
                break
            response = self.article_analysis_runner.analyze_article(candidate.fingerprint)
            outcome = budget.record(response.status)
            if outcome == "completed":
                completed += 1
            else:
                failed += 1

        return BackfillBucketReport(
            bucket=bucket,
            fetch_status=fetch_outcome.fetch_status,
            articles_fetched=fetch_outcome.articles_fetched,
            distinct_publishers=fetch_outcome.distinct_publishers,
            candidates_selected=len(candidates),
            analyses_completed=completed,
            analyses_failed=failed,
            message=message,
        )

    def _fetch_bucket_articles(
        self, constituent: Constituent, bucket: BackfillBucket
    ) -> tuple[list[Article], Literal["ok", "partial", "failed"], str | None]:
        if isinstance(self.historical_news, HistoricalNewsService):
            result, health_list = self.historical_news.fetch_result(
                constituent,
                since=bucket.start,
                until=bucket.end,
                max_articles=self.bucket_max_articles,
            )
            health = health_list[0]
        else:
            result = self.historical_news.fetch_history(
                constituent, bucket.start, bucket.end, self.bucket_max_articles
            )
            health = result.health
        status: Literal["ok", "partial", "failed"] = {
            "healthy": "ok",
            "degraded": "partial",
            "unavailable": "failed",
        }[health.status]
        return list(result.articles), status, health.message

    def _recompute_historical_sentiment(
        self, constituent: Constituent, run_start: datetime
    ) -> list[DailySentiment]:
        """Explicit aggregate -> delete-window -> upsert triad; upsert_sentiments alone never
        produces a daily_sentiment row. Safe to call once over the full backfilled span because
        delete_daily_sentiment and upsert_daily_sentiment always cover the identical span here.
        """

        real_articles = [
            item
            for item in self._read_scored(
                constituent.symbol,
                run_start,
                phase="for the daily-sentiment rebuild",
                state=(
                    "Every planned bucket was fetched, stored and scored (and any analysis within "
                    "the budget ran), but daily_sentiment was NOT deleted or rewritten: it is "
                    "exactly as it was before this run."
                ),
            )
            if not item.is_demo
        ]
        daily = aggregate_daily_sentiment(
            constituent.symbol, real_articles, half_life_hours=self.sentiment_half_life_hours
        )
        self.repository.delete_daily_sentiment(constituent.symbol, run_start.date())
        self.repository.upsert_daily_sentiment(daily)
        return daily

    def _stale_analysis_backlog(self, constituent: Constituent) -> list[Article]:
        all_articles = [
            article
            for article in self.repository.list_articles(constituent.symbol, since=None)
            if not article.is_demo
        ]
        compatible = self.repository.list_article_analyses(
            constituent.symbol,
            since=None,
            limit=_STALE_BACKLOG_QUERY_LIMIT,
            compatibility=self.article_analysis_compatibility,
        )
        compatible_ids = {item.article_id for item in compatible}
        any_analysis = self.repository.list_article_analyses(
            constituent.symbol, since=None, limit=_STALE_BACKLOG_QUERY_LIMIT, compatibility=None
        )
        # Prioritisation only: the *old*, possibly stale analysis's own self-reported magnitude/
        # evidence_strength is used purely to queue the backlog. The new analysis produced by
        # re-running analyze_article is what actually becomes display-compatible; this heuristic
        # never bypasses accepts_for_display's exact-equality check.
        stale_records = {
            item.article_id: item for item in any_analysis if item.article_id not in compatible_ids
        }
        backlog = [article for article in all_articles if article.fingerprint in stale_records]
        backlog.sort(
            key=lambda article: (
                -stale_records[article.fingerprint].event.magnitude,
                -stale_records[article.fingerprint].evidence_strength,
                -article.published_at.timestamp(),
            )
        )
        return backlog


@dataclass
class _BucketFetchOutcome:
    """Result of phase one (fetch/persist/score) for one bucket, consumed by phase two."""

    fetch_status: Literal["ok", "partial", "failed"]
    message: str | None = None
    articles_fetched: int = 0
    distinct_publishers: int = 0
    scored_articles: list[ScoredArticle] = field(default_factory=list)


@dataclass
class _AnalysisBudget:
    """Run-level (not per-bucket) budget/circuit-breaker state, mirroring
    ``service.py::MarketAnalysisService._run_automatic_analysis``'s exact failure philosophy:
    a cached hit never counts against the budget or the circuit breaker; only a genuinely new
    attempt (generated/unavailable/failed/other) does, and only two consecutive non-generated
    attempts trip the breaker.
    """

    max_new_analyses: int
    attempts: int = 0
    consecutive_failures: int = 0
    tripped: bool = False

    @property
    def stopped(self) -> bool:
        return self.tripped or self.attempts >= self.max_new_analyses

    def record(self, status: str) -> Literal["completed", "failed"]:
        if status == "cached":
            self.consecutive_failures = 0
            return "completed"
        self.attempts += 1
        if status == "generated":
            self.consecutive_failures = 0
            return "completed"
        if status in ("unavailable", "failed"):
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 0
        if self.consecutive_failures >= 2:
            self.tripped = True
        return "failed"


def _count_dates_in_bucket(daily: Sequence[DailySentiment], bucket: BackfillBucket) -> int:
    start, end = bucket.start.date(), bucket.end.date()
    return sum(1 for item in daily if start <= item.date < end)


def _join_messages(existing: str | None, addition: str) -> str:
    return f"{existing} {addition}" if existing else addition
