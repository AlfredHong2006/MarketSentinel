"""Application orchestration for one end-to-end analysis request."""

from datetime import timedelta

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.domain import AnalysisResult, PriceHistory, SourceHealth
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.sentiment.finbert import SentimentAnalyzer
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
        sentiment: SentimentAnalyzer,
        prices: PriceProvider,
        repository: SQLiteRepository,
        forecaster: BaselineForecaster,
        news_lookback_days: int = 7,
        news_max_articles: int = 50,
        sentiment_half_life_hours: float = 24.0,
    ) -> None:
        self.constituents = constituents
        self.news = news
        self.sentiment = sentiment
        self.prices = prices
        self.repository = repository
        self.forecaster = forecaster
        self.news_lookback_days = news_lookback_days
        self.news_max_articles = news_max_articles
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
        fetched_articles, news_health = self.news.fetch(
            constituent,
            since=recent_cutoff,
            max_articles=self.news_max_articles,
        )
        self.repository.upsert_articles(fetched_articles)

        already_scored = self.repository.scored_fingerprints(
            article.fingerprint for article in fetched_articles
        )
        pending = [
            article for article in fetched_articles if article.fingerprint not in already_scored
        ]
        newly_scored = self.sentiment.score(pending)
        self.repository.upsert_sentiments(newly_scored)

        aggregation_cutoff = now - timedelta(days=365)
        stored_articles = self.repository.list_scored_articles(
            constituent.symbol,
            since=aggregation_cutoff,
        )
        real_articles = [article for article in stored_articles if not article.is_demo]
        aggregation_articles = real_articles or stored_articles
        daily = aggregate_daily_sentiment(
            constituent.symbol,
            aggregation_articles,
            half_life_hours=self.sentiment_half_life_hours,
        )
        self.repository.upsert_daily_sentiment(daily)
        stored_daily = self.repository.list_daily_sentiment(constituent.symbol)
        forecast = self.forecaster.forecast(price_history.points, stored_daily)

        recent_articles = [
            article for article in aggregation_articles if article.published_at >= recent_cutoff
        ][: self.news_max_articles]
        display_history = PriceHistory(
            symbol=price_history.symbol,
            points=price_history.points[-30:],
            source=price_history.source,
            fetched_at=price_history.fetched_at,
        )
        return AnalysisResult(
            constituent=constituent,
            price_history=display_history,
            articles=recent_articles,
            daily_sentiment=stored_daily,
            forecast=forecast,
            source_health=[price_health, *news_health],
            generated_at=now,
            disclaimer=DISCLAIMER,
        )
