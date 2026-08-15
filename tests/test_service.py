from datetime import UTC, datetime, timedelta

from conftest import make_article, make_constituent, make_price_history

from marketsentinel.domain import IngestionFunnel, NewsFetchResult, SourceHealth
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.storage.sqlite import SQLiteRepository


class FakeConstituents:
    def resolve(self, symbol: str):
        assert symbol == "ACME"
        return make_constituent()


class FakeNews:
    def fetch_result(self, constituent, since, max_articles):
        del constituent, since, max_articles
        article = make_article(published_at=datetime.now(UTC) - timedelta(hours=2))
        health = SourceHealth(
            provider="fake-rss",
            status="healthy",
            records_received=1,
            valid_records=1,
        )
        return NewsFetchResult(
            articles=[article],
            health=health,
            funnel=IngestionFunnel(retrieved=1, relevant=1, unique=1),
        ), [health]


class FakeHistoricalNews:
    name = "fake-gdelt"

    def fetch_history(self, constituent, since, until, max_articles):
        del constituent, since, until, max_articles
        article = make_article(
            title="Acme Corporation announces earnings outlook",
            published_at=datetime.now(UTC) - timedelta(days=12),
            url="https://history.example/acme",
        )
        return NewsFetchResult(
            articles=[article],
            health=SourceHealth(
                provider=self.name,
                status="healthy",
                records_received=1,
                valid_records=1,
            ),
            funnel=IngestionFunnel(retrieved=1, relevant=1, unique=1),
        )


class FakePrices:
    def fetch(self, constituent):
        del constituent
        return make_price_history()


def test_service_runs_complete_vertical_slice_without_network_or_real_model(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    demo_article = make_article(
        title="Acme Corporation demo headline",
        url="https://demo.example/acme",
    ).model_copy(update={"is_demo": True})
    repository.upsert_articles([demo_article])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([demo_article]))
    service = MarketAnalysisService(
        constituents=FakeConstituents(),
        news=FakeNews(),
        historical_news=FakeHistoricalNews(),
        sentiment=StaticSentimentAnalyzer(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
    )

    result = service.analyze("ACME")

    assert result.constituent.symbol == "ACME"
    assert len(result.price_history.points) == 30
    assert len(result.articles) == 2
    assert len(result.daily_sentiment) == 2
    assert result.daily_sentiment[0].article_count == 1
    assert result.forecast.sentiment_features_used is False
    assert result.ingestion_funnel == IngestionFunnel(retrieved=2, relevant=2, unique=2, scored=2)
    assert all(
        article.model_name == "test-static" for article in repository.list_scored_articles("ACME")
    )

    repeated = service.analyze("ACME")
    assert repeated.ingestion_funnel.scored == 0
    assert repeated.ingestion_funnel.database_conflicts == 2
    assert repeated.ingestion_funnel.previously_scored == 2
    assert len(repository.list_scored_articles("ACME")) == 3
