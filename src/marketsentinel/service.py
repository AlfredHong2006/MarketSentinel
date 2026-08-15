"""Application orchestration for one end-to-end analysis request."""

from datetime import timedelta

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.domain import (
    AnalysisResult,
    IngestionFunnel,
    NewsFetchResult,
    PriceHistory,
    SourceHealth,
)
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.normalization import deduplicate_articles, near_duplicate_titles, normalize_url
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
    ) -> None:
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
        candidates = deduplicate_articles([*historical_result.articles, *rss_result.articles])
        known_keys = self.repository.article_dedupe_keys(
            constituent.symbol, since=historical_cutoff
        )
        known_urls = {url for url, _ in known_keys}
        known_titles = [title for _, title in known_keys]
        fetched_articles = [
            article
            for article in candidates
            if normalize_url(article.url) not in known_urls
            and not any(near_duplicate_titles(article.title, title) for title in known_titles)
        ]
        self.repository.upsert_articles(fetched_articles)

        already_scored = self.repository.scored_fingerprints(
            article.fingerprint for article in fetched_articles
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
        stored_daily = [
            item
            for item in self.repository.list_daily_sentiment(constituent.symbol)
            if item.date >= historical_cutoff.date()
        ]
        forecast = self.forecaster.forecast(price_history.points, stored_daily)

        display_articles = real_articles or stored_articles
        display_history = PriceHistory(
            symbol=price_history.symbol,
            points=price_history.points[-30:],
            source=price_history.source,
            fetched_at=price_history.fetched_at,
        )
        return AnalysisResult(
            constituent=constituent,
            price_history=display_history,
            articles=display_articles,
            daily_sentiment=stored_daily,
            forecast=forecast,
            source_health=[price_health, *historical_health, *news_health],
            ingestion_funnel=IngestionFunnel(
                retrieved=historical_result.funnel.retrieved + rss_result.funnel.retrieved,
                relevant=historical_result.funnel.relevant + rss_result.funnel.relevant,
                unique=len(candidates),
                scored=len(already_scored) + len(newly_scored),
            ),
            generated_at=now,
            disclaimer=DISCLAIMER,
        )
