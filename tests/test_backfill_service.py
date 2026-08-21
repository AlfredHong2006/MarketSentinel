from datetime import UTC, datetime, timedelta

from conftest import make_article, make_constituent

from marketsentinel.backfill_service import HistoricalIntelligenceBackfillService
from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleEvidenceReference,
    ClaimAssessments,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    IngestionFunnel,
    NewsFetchResult,
    RelatedCompanyProposals,
    SourceClass,
    SourceHealth,
    TimeHorizon,
    UniverseResult,
)
from marketsentinel.event_analysis import ArticleEventAnalysisService
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.storage.sqlite import SQLiteRepository

NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


class FakeConstituents:
    def resolve(self, symbol: str):
        assert symbol == "ACME"
        return make_constituent()

    def load(self) -> UniverseResult:
        return UniverseResult(
            constituents=[make_constituent()],
            source="test",
            is_fallback=False,
            fetched_at=NOW,
        )


class CountingArticleIntelligenceProvider:
    """Deterministic fake Stage A/B/C provider that counts real (non-cached) calls."""

    model_version = "counting-event-model"

    def __init__(self) -> None:
        self.stage_a_calls = 0

    @property
    def total_calls(self) -> int:
        return self.stage_a_calls

    def extract_event(self, request) -> EventExtraction:
        del request
        self.stage_a_calls += 1
        return EventExtraction(
            event_type=EventType.PARTNERSHIP,
            summary="Acme announced a material commercial partnership.",
            direction=EventDirection.POSITIVE,
            magnitude=0.55,
            time_horizon=TimeHorizon.IMMEDIATE,
            model_confidence=0.85,
            important_claims=["Acme announced a material commercial partnership."],
            positive_channels=["Possible commercial expansion"],
        )

    def assess_claims(self, request) -> ClaimAssessments:
        del request
        return ClaimAssessments()

    def select_related_companies(self, request) -> RelatedCompanyProposals:
        del request
        return RelatedCompanyProposals()


class BucketAwareHistoricalNews:
    """Deterministic, bucket-scoped fake: N distinct articles inside every ``[since, until)``."""

    name = "fake-bucket-news"

    def __init__(
        self,
        articles_per_bucket: int = 3,
        *,
        unavailable_labels: frozenset[str] = frozenset(),
    ) -> None:
        self.articles_per_bucket = articles_per_bucket
        self.unavailable_labels = unavailable_labels
        self.calls: list[tuple[datetime, datetime]] = []

    def fetch_history(self, constituent, since, until, max_articles) -> NewsFetchResult:
        del constituent, max_articles
        self.calls.append((since, until))
        label = f"{since.year:04d}-{since.month:02d}"
        if label in self.unavailable_labels:
            return NewsFetchResult(
                articles=[],
                health=SourceHealth(
                    provider=self.name, status="unavailable", message="simulated provider outage"
                ),
                funnel=IngestionFunnel(),
            )
        span = until - since
        articles = [
            make_article(
                title=f"Acme historical development {label}-{index}",
                published_at=since + span * (index + 1) / (self.articles_per_bucket + 1),
                url=f"https://history.example/{label}-{index}",
                source=f"Historical Wire {index % 2}",
            )
            for index in range(self.articles_per_bucket)
        ]
        return NewsFetchResult(
            articles=articles,
            health=SourceHealth(
                provider=self.name,
                status="healthy",
                records_received=len(articles),
                valid_records=len(articles),
            ),
            funnel=IngestionFunnel(
                retrieved=len(articles), relevant=len(articles), unique=len(articles)
            ),
        )


def _service(
    repository: SQLiteRepository,
    historical_news: BucketAwareHistoricalNews,
    *,
    bucket_candidate_cap: int = 5,
    max_new_analyses: int = 60,
    provider: CountingArticleIntelligenceProvider | None = None,
) -> tuple[HistoricalIntelligenceBackfillService, CountingArticleIntelligenceProvider]:
    constituents = FakeConstituents()
    provider = provider or CountingArticleIntelligenceProvider()
    article_events = ArticleEventAnalysisService(
        repository=repository,
        provider=provider,
        constituents=constituents,
        evidence_limit=5,
    )
    service = HistoricalIntelligenceBackfillService(
        constituents=constituents,
        historical_news=historical_news,
        sentiment=StaticSentimentAnalyzer(),
        repository=repository,
        article_analysis_runner=article_events,
        article_analysis_compatibility=article_events.compatibility,
        bucket_candidate_cap=bucket_candidate_cap,
        max_new_analyses_per_run=max_new_analyses,
    )
    return service, provider


def test_backfill_populates_articles_analyses_and_daily_sentiment_beyond_30_days(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, provider = _service(repository, news)

    report = service.backfill("ACME", now=NOW, horizon_days=90)

    thirty_days_ago = (NOW - timedelta(days=30)).date()
    old_articles = [
        a for a in repository.list_articles("ACME") if a.published_at.date() < thirty_days_ago
    ]
    assert old_articles, "backfill must reach articles older than the live 30-day window"

    old_daily = [
        item for item in repository.list_daily_sentiment("ACME") if item.date < thirty_days_ago
    ]
    assert old_daily, (
        "explicit aggregate->delete->upsert triad must produce >30-day daily_sentiment"
    )

    assert provider.total_calls > 0
    assert report.new_analyses_attempted > 0
    assert report.sentiment_dates_total > 0
    assert len(report.buckets) >= 3
    assert all(bucket.fetch_status == "ok" for bucket in report.buckets)


def test_upsert_sentiments_alone_would_not_have_produced_daily_sentiment(writable_tmp_path) -> None:
    """Guards the exact gap the amendment flagged: article-level scoring is not aggregation."""

    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    article = make_article(published_at=NOW - timedelta(days=60))
    repository.upsert_articles([article])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([article]))

    assert repository.list_daily_sentiment("ACME") == []


def test_bucket_candidate_cap_bounds_candidates_selected_per_bucket(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=8)
    service, _provider = _service(repository, news, bucket_candidate_cap=2, max_new_analyses=200)

    report = service.backfill("ACME", now=NOW, horizon_days=60)

    assert all(bucket.candidates_selected <= 2 for bucket in report.buckets)


def test_rerun_is_idempotent_with_zero_new_llm_calls(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=2)
    service, provider = _service(repository, news, bucket_candidate_cap=2, max_new_analyses=200)

    first = service.backfill("ACME", now=NOW, horizon_days=60)
    calls_after_first = provider.total_calls
    articles_after_first = len(repository.list_articles("ACME"))
    second = service.backfill("ACME", now=NOW, horizon_days=60)

    assert calls_after_first > 0
    assert first.new_analyses_attempted > 0
    assert second.new_analyses_attempted == 0, (
        "a compatible cached analysis must cost zero new calls"
    )
    assert provider.total_calls == calls_after_first
    assert len(repository.list_articles("ACME")) == articles_after_first, (
        "no duplicate article rows"
    )


def test_run_level_budget_stops_new_analysis_but_keeps_fetching_and_scoring(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, provider = _service(repository, news, bucket_candidate_cap=3, max_new_analyses=2)

    report = service.backfill("ACME", now=NOW, horizon_days=120)

    assert report.new_analyses_attempted <= 2
    assert provider.total_calls <= 2
    # Every bucket's articles must still be fetched/persisted/scored even after the budget stops,
    # per the approved plan: cheap plumbing keeps running, only new LLM analysis is bounded.
    assert all(bucket.articles_fetched > 0 for bucket in report.buckets)
    later_buckets_had_no_analysis = any(
        bucket.analyses_completed == 0 and bucket.candidates_selected > 0
        for bucket in report.buckets
    )
    assert later_buckets_had_no_analysis
    thirty_days_ago = (NOW - timedelta(days=30)).date()
    assert any(item.date < thirty_days_ago for item in repository.list_daily_sentiment("ACME"))


def test_partial_and_failed_buckets_are_reported_never_implied_complete(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(
        articles_per_bucket=2, unavailable_labels=frozenset({"2026-06"})
    )
    service, _provider = _service(repository, news)

    report = service.backfill("ACME", now=NOW, horizon_days=90)

    failed = [bucket for bucket in report.buckets if bucket.bucket.label == "2026-06"]
    assert failed, "the simulated outage month must appear in the report"
    assert failed[0].fetch_status == "failed"
    assert failed[0].articles_fetched == 0
    assert failed[0].message is not None
    assert "2026-06" in report.render()
    assert "failed" in report.render()


def test_refresh_evidence_is_free_when_evidence_pool_is_unchanged(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=2)
    service, provider = _service(repository, news, bucket_candidate_cap=2, max_new_analyses=200)
    service.backfill("ACME", now=NOW, horizon_days=60)
    calls_after_backfill = provider.total_calls
    assert calls_after_backfill > 0

    report = service.refresh_evidence("ACME", now=NOW)

    assert report.candidates_checked > 0
    assert report.regenerated == 0
    assert report.cache_hits == report.candidates_checked
    assert provider.total_calls == calls_after_backfill, "unchanged evidence must cost nothing"


def test_refresh_evidence_regenerates_when_a_new_nearby_article_changes_the_pool(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=2)
    service, provider = _service(repository, news, bucket_candidate_cap=2, max_new_analyses=200)
    service.backfill("ACME", now=NOW, horizon_days=60)
    calls_after_backfill = provider.total_calls

    analyzed = repository.list_article_analyses(
        "ACME", since=None, compatibility=service.article_analysis_compatibility
    )
    assert analyzed
    target = analyzed[0]
    nearby = make_article(
        title="A brand new nearby corroborating article",
        published_at=target.source_reference.published_at + timedelta(hours=3),
        url="https://example.com/new-nearby",
        source="A Brand New Publisher",
    )
    repository.upsert_articles([nearby])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([nearby]))

    report = service.refresh_evidence("ACME", now=NOW)

    assert report.regenerated >= 1, "a new, temporally close article must change the evidence pool"
    assert provider.total_calls > calls_after_backfill


def test_reanalyze_stale_only_targets_articles_with_no_compatible_analysis(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    news = BucketAwareHistoricalNews(articles_per_bucket=2)
    service, provider = _service(repository, news, bucket_candidate_cap=2, max_new_analyses=200)

    service.backfill("ACME", now=NOW, horizon_days=45)
    analyzed_after_backfill = provider.total_calls
    assert analyzed_after_backfill > 0

    # A stored, but version-incompatible, analysis for one extra article -- must be caught up,
    # never displayed as-is (accepts_for_display's exact-equality guarantee is never touched here).
    stale_article = make_article(
        title="Acme legacy stored analysis with a stale prompt version",
        published_at=NOW - timedelta(days=40),
        url="https://history.example/legacy",
    )
    repository.upsert_articles([stale_article])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([stale_article]))

    stale_analysis = ArticleAnalysis(
        article_id=stale_article.fingerprint,
        source_reference=ArticleEvidenceReference(
            article_id=stale_article.fingerprint,
            title=stale_article.title,
            publisher=stale_article.source,
            published_at=stale_article.published_at,
            url=stale_article.url,
        ),
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=EventExtraction(
            event_type=EventType.REGULATION,
            summary="A stale-version stored regulatory event.",
            direction=EventDirection.NEGATIVE,
            magnitude=0.6,
            time_horizon=TimeHorizon.MONTHS,
            model_confidence=0.8,
            important_claims=["Stale claim."],
        ),
        evidence_count=0,
        evidence_strength=0.6,
        evidence_fingerprint="stale-evidence",
        model_version="an-old-model-version",
        stage_a_prompt_version="event-extraction-v1",
        stage_b_prompt_version="claim-evidence-v0",
        stage_c_prompt_version="related-company-v0",
        schema_version="article-intelligence-v0",
        analysis_created_at=NOW,
    )
    repository.store_article_analysis(stale_analysis, "stale-cache-version")

    report = service.reanalyze_stale("ACME", now=NOW)

    assert report.backlog_size == 1
    assert report.reanalyzed == 1
    assert provider.total_calls == analyzed_after_backfill + 1
    compatible_now = repository.list_article_analyses(
        "ACME", since=None, compatibility=service.article_analysis_compatibility
    )
    assert stale_article.fingerprint in {item.article_id for item in compatible_now}
