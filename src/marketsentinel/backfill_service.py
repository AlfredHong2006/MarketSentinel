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
    BackfillRunReport,
    EvidenceRefreshReport,
    StaleBacklogReport,
    plan_backfill_buckets,
)
from marketsentinel.sentiment.finbert import SentimentAnalyzer
from marketsentinel.service import ArticleAnalysisRunner
from marketsentinel.sources.historical import HistoricalNewsProvider, HistoricalNewsService
from marketsentinel.storage.sqlite import SQLiteRepository

# list_scored_articles/list_article_analyses default to smaller limits sized for one live
# request's ~30-day window; a full-horizon backfill query needs generous, explicit caps of its
# own so a large stored corpus is never silently truncated (the same class of fix as service.py's
# _STORED_ANALYSES_LIMIT).
_SENTIMENT_AGGREGATION_LIMIT = 5_000
_STALE_BACKLOG_QUERY_LIMIT = 5_000


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
    ) -> None:
        if not 1 <= bucket_candidate_cap <= 40:
            raise ValueError("bucket_candidate_cap must be between 1 and 40")
        if max_new_analyses_per_run < 0:
            raise ValueError("max_new_analyses_per_run must not be negative")
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

    def backfill(self, symbol: str, *, now: datetime, horizon_days: int = 366) -> BackfillRunReport:
        """Populate historical articles/sentiment/analyses for one ticker. Idempotent to re-run.

        Two phases, not one pass per bucket: every bucket's articles are fetched, persisted, and
        scored FIRST; only then does candidate selection/analysis begin. analyze_article's own
        cache key includes an evidence_fingerprint over same-ticker comparison articles already
        in storage at call time -- if analysis started before later buckets were persisted, a
        first run would see a smaller evidence pool than a second run does, changing the
        fingerprint and busting the cache even though nothing genuinely changed. Fetching
        everything before analyzing anything keeps that pool identical run over run.
        """

        constituent = self.constituents.resolve(symbol)
        buckets = plan_backfill_buckets(now, horizon_days=horizon_days)

        fetch_outcomes = [self._fetch_and_score_bucket(constituent, bucket) for bucket in buckets]

        budget = _AnalysisBudget(self.max_new_analyses_per_run)
        bucket_reports = [
            self._select_and_analyze_bucket(constituent, bucket, outcome, budget)
            for bucket, outcome in zip(buckets, fetch_outcomes, strict=True)
        ]

        run_start = now - timedelta(days=horizon_days)
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
        self, constituent: Constituent, bucket: BackfillBucket
    ) -> "_BucketFetchOutcome":
        try:
            articles, fetch_status, health_message = self._fetch_bucket_articles(
                constituent, bucket
            )
        except Exception as exc:  # defensive: providers normally report health, never raise
            return _BucketFetchOutcome(fetch_status="failed", message=str(exc))

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
            for item in self.repository.list_scored_articles(
                constituent.symbol, since=bucket.start, limit=_SENTIMENT_AGGREGATION_LIMIT
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
            for item in self.repository.list_scored_articles(
                constituent.symbol, since=run_start, limit=_SENTIMENT_AGGREGATION_LIMIT
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
