from datetime import UTC, datetime, timedelta

from conftest import make_article, make_constituent, make_price_history

from marketsentinel.domain import SourceHealth
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.storage.sqlite import SQLiteRepository


class FakeConstituents:
    def resolve(self, symbol: str):
        assert symbol == "ACME"
        return make_constituent()


class FakeNews:
    def fetch(self, constituent, since, max_articles):
        del constituent, since, max_articles
        article = make_article(published_at=datetime.now(UTC) - timedelta(hours=2))
        health = SourceHealth(
            provider="fake-rss",
            status="healthy",
            records_received=1,
            valid_records=1,
        )
        return [article], [health]


class FakePrices:
    def fetch(self, constituent):
        del constituent
        return make_price_history()


def test_service_runs_complete_vertical_slice_without_network_or_real_model(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    demo_article = make_article(title="Acme Corporation demo headline").model_copy(
        update={"is_demo": True}
    )
    repository.upsert_articles([demo_article])
    repository.upsert_sentiments(StaticSentimentAnalyzer().score([demo_article]))
    service = MarketAnalysisService(
        constituents=FakeConstituents(),
        news=FakeNews(),
        sentiment=StaticSentimentAnalyzer(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
    )

    result = service.analyze("ACME")

    assert result.constituent.symbol == "ACME"
    assert len(result.price_history.points) == 30
    assert len(result.articles) == 1
    assert len(result.daily_sentiment) == 1
    assert result.daily_sentiment[0].article_count == 1
    assert result.forecast.sentiment_features_used is False
    assert all(
        article.model_name == "test-static" for article in repository.list_scored_articles("ACME")
    )
