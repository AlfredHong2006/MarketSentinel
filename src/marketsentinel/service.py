"""Application orchestration for one end-to-end analysis request."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.analysis_candidates import select_analysis_candidates_with_diagnostics
from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.domain import (
    AnalysisResult,
    Article,
    ArticleAnalysisResponse,
    AutomaticAnalysisDiagnostics,
    CompanyOverview,
    Constituent,
    NewsFetchResult,
    PriceHistory,
    SourceHealth,
)
from marketsentinel.errors import ProviderError
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.normalization import (
    deduplicate_with_diagnostics,
    deduplication_reason,
)
from marketsentinel.overview import build_company_overview
from marketsentinel.risk_scoring import rank_company_risks
from marketsentinel.sentiment.finbert import SentimentAnalyzer
from marketsentinel.sources.historical import HistoricalNewsProvider, HistoricalNewsService
from marketsentinel.sources.news import NewsService
from marketsentinel.sources.prices import PriceProvider
from marketsentinel.storage.sqlite import SQLiteRepository
from marketsentinel.timeutils import utc_now

DISCLAIMER = (
    "MarketSentinel is an educational research tool. Sentiment scores and forecast probabilities "
    "are not financial advice and do not predict exact prices."
)

# repository.list_article_analyses defaults to limit=100, which would silently drop the oldest
# distinct-compatible analyses once historical backfill (or continued live use) grows a ticker's
# analyzed-article count past it -- from analyzed_events, intelligence_events, AND the risk-scoring
# input, not just the display cap. This is a purely quantitative ceiling raise; every filter,
# ordering, and compatibility rule inside list_article_analyses is unchanged.
_STORED_ANALYSES_LIMIT = 500

# repository.list_daily_sentiment defaults to limit=365; widened slightly to comfortably cover a
# 366-day display window before client-side date filtering.
_STORED_DAILY_SENTIMENT_LIMIT = 370


class ArticleAnalysisRunner(Protocol):
    def analyze_article(self, article_id: str) -> ArticleAnalysisResponse: ...


class MarketAnalysisService:
    def __init__(
        self,
        constituents: WikipediaConstituentService,
        news: NewsService,
        historical_news: HistoricalNewsProvider | HistoricalNewsService | None,
        sentiment: SentimentAnalyzer,
        prices: PriceProvider,
        repository: SQLiteRepository,
        forecaster: BaselineForecaster,
        news_lookback_days: int = 7,
        news_max_articles: int = 50,
        historical_news_days: int = 30,
        historical_news_max_articles: int = 180,
        sentiment_half_life_hours: float = 24.0,
        article_analysis_compatibility: ArticleAnalysisCompatibility | None = None,
        article_analysis_runner: ArticleAnalysisRunner | None = None,
        analysis_auto_candidates: int = 15,
        analysis_auto_max_new_per_run: int = 6,
    ) -> None:
        if not 0 <= analysis_auto_candidates <= 40:
            raise ValueError("analysis_auto_candidates must be between 0 and 40")
        if not 0 <= analysis_auto_max_new_per_run <= 15:
            raise ValueError("analysis_auto_max_new_per_run must be between 0 and 15")
        self.constituents = constituents
        self.news = news
        self.historical_news = historical_news
        self.sentiment = sentiment
        self.prices = prices
        self.repository = repository
        self.forecaster = forecaster
        self.news_lookback_days = news_lookback_days
        self.news_max_articles = news_max_articles
        self.historical_news_days = historical_news_days
        self.historical_news_max_articles = historical_news_max_articles
        self.sentiment_half_life_hours = sentiment_half_life_hours
        self.article_analysis_compatibility = article_analysis_compatibility
        self.article_analysis_runner = article_analysis_runner
        self.analysis_auto_candidates = analysis_auto_candidates
        self.analysis_auto_max_new_per_run = analysis_auto_max_new_per_run

    def analyze(self, symbol: str) -> AnalysisResult:
        constituent = self.constituents.resolve(symbol)
        price_history = self.prices.fetch(constituent)
        price_health = SourceHealth(
            provider=price_history.source,
            status="healthy",
            records_received=len(price_history.points),
            valid_records=len(price_history.points),
        )

        now = utc_now()
        recent_cutoff = now - timedelta(days=self.news_lookback_days)
        historical_cutoff = now - timedelta(days=self.historical_news_days)
        if self.historical_news is None:
            historical_result = NewsFetchResult(
                articles=[],
                health=SourceHealth(
                    provider="Historical news provider",
                    status="unavailable",
                    message="Historical backfill is not configured.",
                ),
            )
        elif isinstance(self.historical_news, HistoricalNewsService):
            historical_result, historical_health = self.historical_news.fetch_result(
                constituent,
                since=historical_cutoff,
                until=now,
                max_articles=self.historical_news_max_articles,
            )
        else:
            historical_result = self.historical_news.fetch_history(
                constituent,
                since=historical_cutoff,
                until=now,
                max_articles=self.historical_news_max_articles,
            )
            historical_health = [historical_result.health]
        if self.historical_news is None:
            historical_health = [historical_result.health]
        rss_result, news_health = self.news.fetch_result(
            constituent,
            since=recent_cutoff,
            max_articles=self.news_max_articles,
        )
        provider_funnel = historical_result.funnel.merged(rss_result.funnel)
        combined = deduplicate_with_diagnostics([*historical_result.articles, *rss_result.articles])
        stored_candidates = self.repository.list_articles(
            constituent.symbol, since=historical_cutoff
        )
        fetched_articles = []
        database_matches = []
        for article in combined.articles:
            match = next(
                (
                    stored
                    for stored in stored_candidates
                    if deduplication_reason(article, stored) is not None
                ),
                None,
            )
            if match is None:
                fetched_articles.append(article)
            else:
                database_matches.append(match)
        self.repository.upsert_articles(fetched_articles)

        already_scored = self.repository.scored_fingerprints(
            article.fingerprint for article in fetched_articles
        )
        previously_scored = self.repository.scored_fingerprints(
            article.fingerprint for article in database_matches
        )
        pending = [
            article for article in fetched_articles if article.fingerprint not in already_scored
        ]
        newly_scored = self.sentiment.score(pending)
        self.repository.upsert_sentiments(newly_scored)

        stored_articles = self.repository.list_scored_articles(
            constituent.symbol,
            since=historical_cutoff,
        )
        real_articles = [article for article in stored_articles if not article.is_demo]
        daily = aggregate_daily_sentiment(
            constituent.symbol,
            real_articles,
            half_life_hours=self.sentiment_half_life_hours,
        )
        self.repository.delete_daily_sentiment(constituent.symbol, historical_cutoff.date())
        self.repository.upsert_daily_sentiment(daily)
        # One read serves both windows: the forecaster keeps its existing short recompute window
        # unchanged, while the returned AnalysisResult can additionally surface older sentiment
        # rows already produced by a historical backfill, without ever filling or interpolating.
        all_recent_daily = self.repository.list_daily_sentiment(
            constituent.symbol, limit=_STORED_DAILY_SENTIMENT_LIMIT
        )
        stored_daily = [item for item in all_recent_daily if item.date >= historical_cutoff.date()]
        forecast = self.forecaster.forecast(price_history.points, stored_daily)
        sentiment_display_cutoff = now - timedelta(days=366)
        stored_daily_for_display = [
            item for item in all_recent_daily if item.date >= sentiment_display_cutoff.date()
        ]

        display_articles = real_articles or stored_articles
        latest_price_date = price_history.points[-1].date
        display_price_cutoff = latest_price_date - timedelta(days=366)
        display_history = PriceHistory(
            symbol=price_history.symbol,
            points=[point for point in price_history.points if point.date >= display_price_cutoff],
            source=price_history.source,
            fetched_at=price_history.fetched_at,
        )
        automatic_analysis = self._run_automatic_analysis(real_articles, constituent, now)
        # Reload after the runner loop: analyses generated by this request must be returned now.
        stored_analyses = self.repository.list_article_analyses(
            constituent.symbol,
            since=now - timedelta(days=366),
            limit=_STORED_ANALYSES_LIMIT,
            compatibility=self.article_analysis_compatibility,
        )
        analyzed_events = [item.to_analyzed_event() for item in stored_analyses]
        intelligence_events = [item.to_company_intelligence_event() for item in stored_analyses]
        # Deterministic and recomputed per request, so a policy change needs no migration and
        # an analysis generated above can influence Top Risks in this same response.
        risks = rank_company_risks(stored_analyses, now=now)
        return AnalysisResult(
            constituent=constituent,
            price_history=display_history,
            articles=display_articles,
            daily_sentiment=stored_daily_for_display,
            analyzed_events=analyzed_events,
            intelligence_events=intelligence_events,
            forecast=forecast,
            source_health=[price_health, *historical_health, *news_health],
            ingestion_funnel=provider_funnel.model_copy(
                update={
                    "exact_duplicates": provider_funnel.exact_duplicates
                    + combined.exact_duplicates,
                    "canonical_url_duplicates": provider_funnel.canonical_url_duplicates
                    + combined.canonical_url_duplicates,
                    "near_title_duplicates": provider_funnel.near_title_duplicates
                    + combined.near_title_duplicates,
                    "database_conflicts": len(database_matches),
                    "unique": len(fetched_articles),
                    "scored": len(newly_scored),
                    "previously_scored": len(previously_scored) + len(already_scored),
                }
            ),
            automatic_analysis=automatic_analysis,
            top_risks=list(risks.top_risks),
            risk_diagnostics=risks.diagnostics,
            generated_at=now,
            disclaimer=DISCLAIMER,
        )

    def read_stored(self, symbol: str) -> CompanyOverview:
        """Assemble the Company Overview from what is already stored, and nothing else.

        Deliberately not ``analyze``. This path fetches no news, scores no sentiment, runs no
        article analysis, and writes no row: it reads the SQLite corpus and recomputes the
        deterministic layers over it, so opening the product costs neither ingestion nor LLM spend
        and repeated reads cannot grow the stored corpus. Refreshing coverage stays an explicit
        request to ``analyze``.

        Price history is the one thing that cannot be served this way, because it is not persisted.
        It is fetched live, and a failed fetch reports an unavailable chart rather than
        substituting a made-up series.

        The constituent is resolved from the local cache only. A read must never quietly reach
        Wikipedia, and constituent identity changes far more slowly than the refresh interval.
        """

        constituent = self.constituents.resolve_cached(symbol)
        now = utc_now()
        coverage_cutoff = now - timedelta(days=self.historical_news_days)
        display_cutoff = now - timedelta(days=366)

        price_history: PriceHistory | None = None
        price_message: str | None = None
        try:
            fetched = self.prices.fetch(constituent)
        except ProviderError as exc:
            price_message = str(exc)
        else:
            display_price_cutoff = fetched.points[-1].date - timedelta(days=366)
            price_history = PriceHistory(
                symbol=fetched.symbol,
                points=[point for point in fetched.points if point.date >= display_price_cutoff],
                source=fetched.source,
                fetched_at=fetched.fetched_at,
            )

        stored_articles = self.repository.list_scored_articles(
            constituent.symbol, since=coverage_cutoff
        )
        real_articles = [article for article in stored_articles if not article.is_demo]
        display_articles = real_articles or stored_articles
        stored_daily = [
            item
            for item in self.repository.list_daily_sentiment(
                constituent.symbol, limit=_STORED_DAILY_SENTIMENT_LIMIT
            )
            if item.date >= display_cutoff.date()
        ]
        stored_analyses = self.repository.list_article_analyses(
            constituent.symbol,
            since=display_cutoff,
            limit=_STORED_ANALYSES_LIMIT,
            compatibility=self.article_analysis_compatibility,
        )
        return build_company_overview(
            constituent=constituent,
            price_history=price_history,
            price_message=price_message,
            articles=display_articles,
            daily_sentiment=stored_daily,
            analyzed_events=[item.to_analyzed_event() for item in stored_analyses],
            intelligence_events=[item.to_company_intelligence_event() for item in stored_analyses],
            risks=rank_company_risks(stored_analyses, now=now),
            coverage_window_days=self.historical_news_days,
            generated_at=now,
            disclaimer=DISCLAIMER,
            data_source="stored",
        )

    def _run_automatic_analysis(
        self,
        articles: Sequence[Article],
        constituent: Constituent,
        now: datetime,
    ) -> AutomaticAnalysisDiagnostics:
        selection = select_analysis_candidates_with_diagnostics(
            articles,
            now,
            self.analysis_auto_candidates,
            subject_company=constituent,
        )
        candidates = selection.candidates
        if self.article_analysis_runner is None or self.analysis_auto_max_new_per_run == 0:
            return selection.diagnostics.model_copy(update={"budget_skipped": len(candidates)})

        cached = newly_generated = failed = unavailable = new_attempts = 0
        consecutive_failures = 0
        budget_skipped = 0
        circuit_breaker_tripped = False
        for index, candidate in enumerate(candidates):
            if new_attempts >= self.analysis_auto_max_new_per_run:
                budget_skipped = len(candidates) - index
                break

            response = self.article_analysis_runner.analyze_article(candidate.fingerprint)
            if response.status == "cached":
                cached += 1
                consecutive_failures = 0
                continue
            if response.status == "generated":
                newly_generated += 1
                new_attempts += 1
                consecutive_failures = 0
                continue
            if response.status == "unavailable":
                unavailable += 1
                new_attempts += 1
                consecutive_failures += 1
            elif response.status == "failed":
                failed += 1
                new_attempts += 1
                consecutive_failures += 1
            else:
                failed += 1
                new_attempts += 1
                consecutive_failures = 0

            if consecutive_failures >= 2:
                circuit_breaker_tripped = True
                budget_skipped = len(candidates) - index - 1
                break

        return selection.diagnostics.model_copy(
            update={
                "cached": cached,
                "newly_generated": newly_generated,
                "failed": failed,
                "unavailable": unavailable,
                "budget_skipped": budget_skipped,
                "circuit_breaker_tripped": circuit_breaker_tripped,
            }
        )
