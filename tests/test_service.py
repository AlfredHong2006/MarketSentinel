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
from marketsentinel.dashboard_risks import compatible_top_risks, prepare_top_risk_rows
from marketsentinel.domain import (
    ArticleAnalysis,
    ArticleAnalysisResponse,
    ArticleEvidenceReference,
    ClaimAssessments,
    CompanyReference,
    EventDirection,
    EventExtraction,
    EventType,
    IngestionFunnel,
    NewsFetchResult,
    RelatedCompanyProposals,
    RiskTheme,
    SourceClass,
    SourceHealth,
    TimeHorizon,
    UniverseResult,
)
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
    ArticleEventAnalysisService,
)
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.storage.sqlite import SQLiteRepository


class FakeConstituents:
    def resolve(self, symbol: str):
        assert symbol == "ACME"
        return make_constituent()

    def load(self) -> UniverseResult:
        return UniverseResult(
            constituents=[make_constituent()],
            source="test",
            is_fallback=False,
            fetched_at=datetime.now(UTC),
        )


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


class StoringArticleAnalysisRunner:
    def __init__(
        self,
        repository: SQLiteRepository,
        model_version: str,
        *,
        negative_channels: tuple[str, ...] = (),
        time_horizon: TimeHorizon = TimeHorizon.IMMEDIATE,
    ) -> None:
        self.repository = repository
        self.model_version = model_version
        self.negative_channels = negative_channels
        self.time_horizon = time_horizon
        self.calls: list[str] = []

    def analyze_article(self, article_id: str) -> ArticleAnalysisResponse:
        self.calls.append(article_id)
        article = self.repository.get_article(article_id)
        assert article is not None
        analysis = ArticleAnalysis(
            article_id=article.fingerprint,
            source_reference=ArticleEvidenceReference(
                article_id=article.fingerprint,
                title=article.title,
                publisher=article.source,
                published_at=article.published_at,
                url=article.url,
            ),
            source_class=SourceClass.MAJOR_FINANCIAL_NEWS,
            subject_company=CompanyReference(symbol="ACME", name="Acme Corporation"),
            event=EventExtraction(
                event_type=EventType.PARTNERSHIP,
                summary=f"Acme announced a material partnership: {article.title}",
                direction=EventDirection.POSITIVE,
                magnitude=0.55,
                time_horizon=self.time_horizon,
                model_confidence=0.85,
                important_claims=["Acme announced a material partnership."],
                positive_channels=["Possible commercial expansion"],
                negative_channels=list(self.negative_channels),
            ),
            evidence_count=0,
            evidence_strength=0.7,
            evidence_fingerprint=f"evidence-{article.fingerprint[:16]}",
            model_version=self.model_version,
            stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
            stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
            stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
            schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
            analysis_created_at=datetime.now(UTC),
        )
        self.repository.store_article_analysis(analysis, f"fake-{article.fingerprint}")
        return ArticleAnalysisResponse(
            article_id=article_id,
            status="generated",
            analysis=analysis,
        )


class CountingArticleIntelligenceProvider:
    model_version = "counting-event-model"

    def __init__(self) -> None:
        self.stage_a_calls = 0
        self.stage_b_calls = 0
        self.stage_c_calls = 0

    @property
    def total_calls(self) -> int:
        return self.stage_a_calls + self.stage_b_calls + self.stage_c_calls

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
        self.stage_b_calls += 1
        return ClaimAssessments()

    def select_related_companies(self, request) -> RelatedCompanyProposals:
        del request
        self.stage_c_calls += 1
        return RelatedCompanyProposals()


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


def test_automatic_candidate_analysis_reaches_same_company_response(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    model_version = "test-event-model"
    compatibility = ArticleAnalysisCompatibility(
        model_version=model_version,
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )
    runner = StoringArticleAnalysisRunner(repository, model_version)
    service = MarketAnalysisService(
        constituents=FakeConstituents(),
        news=FakeNews(),
        historical_news=FakeHistoricalNews(),
        sentiment=StaticSentimentAnalyzer(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=compatibility,
        article_analysis_runner=runner,
        analysis_auto_candidates=15,
        analysis_auto_max_new_per_run=6,
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
    assert runner.calls
    assert payload["automatic_analysis"]["newly_generated"] == len(runner.calls)
    assert set(runner.calls).issubset(
        {item["article_id"] for item in payload["intelligence_events"]}
    )
    assert set(runner.calls).issubset({item["article_id"] for item in payload["analyzed_events"]})
    stored = repository.list_article_analyses("ACME", compatibility=compatibility)
    assert stored
    assert all(item.stage_a_prompt_version == STAGE_A_PROMPT_VERSION for item in stored)
    cards = prepare_todays_intelligence(
        compatible_intelligence_events(payload["intelligence_events"])
    )
    assert set(runner.calls).intersection(card.event.article_id for card in cards)


def test_repeat_company_analysis_uses_real_exact_article_analysis_cache(
    writable_tmp_path,
) -> None:
    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    constituents = FakeConstituents()
    provider = CountingArticleIntelligenceProvider()
    article_events = ArticleEventAnalysisService(
        repository=repository,
        provider=provider,
        constituents=constituents,
        evidence_limit=5,
    )
    service = MarketAnalysisService(
        constituents=constituents,
        news=FakeNews(),
        historical_news=FakeHistoricalNews(),
        sentiment=StaticSentimentAnalyzer(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=article_events.compatibility,
        article_analysis_runner=article_events,
        analysis_auto_candidates=15,
        analysis_auto_max_new_per_run=6,
    )
    app = create_app(
        services=Services(
            repository=repository,
            constituents=constituents,
            analysis=service,
            article_events=article_events,
        )
    )

    with TestClient(app) as client:
        first = client.post("/api/v1/analyze", json={"symbol": "ACME"})
        provider_calls_after_first = provider.total_calls
        second = client.post("/api/v1/analyze", json={"symbol": "ACME"})

    first.raise_for_status()
    second.raise_for_status()
    first_diagnostics = first.json()["automatic_analysis"]
    second_diagnostics = second.json()["automatic_analysis"]
    assert first_diagnostics["newly_generated"] > 0
    assert provider_calls_after_first > 0
    assert second_diagnostics["cached"] == first_diagnostics["newly_generated"]
    assert second_diagnostics["newly_generated"] == 0
    assert provider.total_calls == provider_calls_after_first


def test_newly_stored_analysis_populates_top_risks_in_the_same_response(
    writable_tmp_path,
) -> None:
    """A downside channel analysed during this request must reach top_risks in this response."""

    repository = SQLiteRepository(writable_tmp_path / "market.db")
    repository.initialize()
    model_version = "test-event-model"
    compatibility = ArticleAnalysisCompatibility(
        model_version=model_version,
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )
    runner = StoringArticleAnalysisRunner(
        repository,
        model_version,
        negative_channels=("increases capital committed before utilisation is proven",),
        time_horizon=TimeHorizon.MONTHS,
    )
    service = MarketAnalysisService(
        constituents=FakeConstituents(),
        news=FakeNews(),
        historical_news=FakeHistoricalNews(),
        sentiment=StaticSentimentAnalyzer(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=compatibility,
        article_analysis_runner=runner,
        analysis_auto_candidates=15,
        analysis_auto_max_new_per_run=6,
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
    assert runner.calls
    assert payload["automatic_analysis"]["newly_generated"] == len(runner.calls)

    # The JSON boundary must survive: RankedRisk is a flat non-strict payload.
    risks = compatible_top_risks(payload["top_risks"])
    assert risks, "a stored downside channel should produce at least one ranked risk"
    rows = prepare_top_risk_rows(risks)
    assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
    assert len(rows) <= 4

    capital = next(risk for risk in risks if risk.theme is RiskTheme.CAPITAL_ALLOCATION)
    assert capital.concern_index >= 1
    assert capital.band in {"Severe", "Elevated", "Moderate", "Watch"}
    assert capital.primary_article_id in runner.calls
    assert capital.supporting_article_ids
    assert capital.summary == "increases capital committed before utilisation is proven"

    diagnostics = payload["risk_diagnostics"]
    assert diagnostics["eligible_analyses"] >= 1
    assert diagnostics["prospective_signals"] >= 1
    assert diagnostics["themes_ranked"] == len(risks)
