from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conftest import make_article, make_constituent, make_price_history
from fastapi.testclient import TestClient

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.api.app import Services, create_app
from marketsentinel.dashboard_intelligence import (
    compatible_intelligence_events,
    prepare_todays_intelligence,
)
from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleEvidenceReference,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    IngestionFunnel,
    NewsFetchResult,
    SourceClass,
    SourceHealth,
    TimeHorizon,
)
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
)
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
    assert len(result.price_history.points) > 250
    assert result.price_history.points[0].date >= (
        result.price_history.points[-1].date - timedelta(days=366)
    )
    six_month_cutoff = result.price_history.points[-1].date - timedelta(days=183)
    assert (
        len([point for point in result.price_history.points if point.date >= six_month_cutoff])
        > 120
    )
    assert len(result.articles) == 2
    assert len(result.daily_sentiment) == 2
    assert result.analyzed_events == []
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


def test_stored_compatible_meaningful_analysis_reaches_todays_intelligence(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    article = make_article(
        title="Acme accelerates robotics collaboration",
        source="Test Financial Wire",
    )
    repository.upsert_articles([article])
    model_version = "test-event-model"
    compatibility = ArticleAnalysisCompatibility(
        model_version=model_version,
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )
    reference = ArticleEvidenceReference(
        article_id=article.fingerprint,
        title=article.title,
        publisher=article.source,
        published_at=article.published_at,
        url=article.url,
    )
    analysis = ArticleAnalysis(
        article_id=article.fingerprint,
        source_reference=reference,
        source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
        subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
        event=EventExtraction(
            event_type=EventType.PARTNERSHIP,
            summary="Acme announced a material robotics collaboration.",
            direction=EventDirection.POSITIVE,
            magnitude=0.55,
            time_horizon=TimeHorizon.IMMEDIATE,
            model_confidence=0.85,
            important_claims=["Acme announced a robotics collaboration."],
            positive_channels=["Possible commercial expansion"],
        ),
        evidence_count=0,
        evidence_strength=0.7,
        evidence_fingerprint="test-evidence",
        model_version=model_version,
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
        analysis_created_at=datetime.now(UTC),
    )
    repository.store_article_analysis(analysis, "current-compatible-analysis")
    service = MarketAnalysisService(
        constituents=FakeConstituents(),
        news=FakeNews(),
        historical_news=FakeHistoricalNews(),
        sentiment=StaticSentimentAnalyzer(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=compatibility,
    )
    app = create_app(
        services=Services(
            repository=repository,
            constituents=FakeConstituents(),
            analysis=service,
            article_events=SimpleNamespace(),
        )
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/analyze", json={"symbol": "ACME"})

    response.raise_for_status()
    payload = response.json()
    assert analysis.article_id in {item["article_id"] for item in payload["analyzed_events"]}
    assert analysis.article_id in {item["article_id"] for item in payload["intelligence_events"]}
    compatible = compatible_intelligence_events(payload["intelligence_events"])
    cards = prepare_todays_intelligence(compatible)
    assert analysis.article_id in {card.event.article_id for card in cards}
