"""FastAPI boundary for constituent search and full MarketSentinel analysis."""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.config import Settings, get_settings
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.domain import (
    AnalysisResult,
    ArticleAnalysisResponse,
    CompanyOverview,
    UniverseResult,
)
from marketsentinel.errors import (
    ConstituentNotFoundError,
    ForecastError,
    ProviderError,
    SentimentModelError,
)
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
    ArticleEventAnalysisService,
    OpenAIArticleIntelligenceProvider,
    UnavailableArticleAnalysisProvider,
)
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.sentiment.finbert import FinBertAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.sources.historical import (
    GdeltHistoricalNewsProvider,
    GoogleNewsHistoricalProvider,
    HistoricalNewsService,
)
from marketsentinel.sources.news import DemoNewsProvider, GoogleNewsRssProvider, NewsService
from marketsentinel.sources.prices import YFinancePriceProvider
from marketsentinel.storage.sqlite import SQLiteRepository

LOGGER = logging.getLogger(__name__)


class AnalysisRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)


class ArticleAnalysisRequest(BaseModel):
    article_id: str = Field(min_length=1, max_length=128)


@dataclass
class Services:
    repository: SQLiteRepository
    constituents: WikipediaConstituentService
    analysis: MarketAnalysisService
    article_events: ArticleEventAnalysisService


def build_services(settings: Settings) -> Services:
    repository = SQLiteRepository(settings.database_path)
    constituents = WikipediaConstituentService(
        cache_path=settings.constituent_cache_path,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )
    rss = GoogleNewsRssProvider(
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )
    demo = DemoNewsProvider(settings.demo_news_path) if settings.allow_demo_fallback else None
    news = NewsService(primary=rss, demo_fallback=demo)
    historical_news = HistoricalNewsService(
        primary=GdeltHistoricalNewsProvider(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
            window_days=settings.historical_gdelt_window_days,
            request_interval_seconds=settings.historical_gdelt_request_interval_seconds,
        ),
        rss_fallback=GoogleNewsHistoricalProvider(
            timeout_seconds=settings.request_timeout_seconds,
            user_agent=settings.user_agent,
        ),
    )
    sentiment = FinBertAnalyzer(
        model_name=settings.finbert_model,
        device=settings.finbert_device,
        batch_size=settings.finbert_batch_size,
        hf_token=settings.hf_token,
    )
    provider = (
        OpenAIArticleIntelligenceProvider(
            api_key=settings.llm_api_key,
            model_version=settings.llm_model,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
        )
        if settings.llm_api_key
        else UnavailableArticleAnalysisProvider()
    )
    article_events = ArticleEventAnalysisService(
        repository=repository,
        provider=provider,
        constituents=constituents,
        evidence_limit=settings.article_analysis_evidence_limit,
    )
    analysis = MarketAnalysisService(
        constituents=constituents,
        news=news,
        historical_news=historical_news,
        sentiment=sentiment,
        prices=YFinancePriceProvider(),
        repository=repository,
        forecaster=BaselineForecaster(),
        news_lookback_days=settings.news_lookback_days,
        news_max_articles=settings.news_max_articles,
        historical_news_days=settings.historical_news_days,
        historical_news_max_articles=settings.historical_news_max_articles,
        sentiment_half_life_hours=settings.sentiment_half_life_hours,
        article_analysis_compatibility=ArticleAnalysisCompatibility(
            model_version=settings.llm_model,
            stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
            stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
            stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
            schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
        ),
        article_analysis_runner=article_events if settings.llm_api_key else None,
        analysis_auto_candidates=settings.analysis_auto_candidates,
        analysis_auto_max_new_per_run=settings.analysis_auto_max_new_per_run,
    )
    return Services(
        repository=repository,
        constituents=constituents,
        analysis=analysis,
        article_events=article_events,
    )


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    services = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        services.repository.initialize()
        yield

    app = FastAPI(
        title="MarketSentinel API",
        version="0.1.0",
        description=(
            "Recent financial-news sentiment and an experimental five-session direction baseline. "
            "Not financial advice."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "MarketSentinel"}

    @app.get("/api/v1/constituents/search", response_model=UniverseResult)
    def search_constituents(
        q: str = Query(default="", max_length=100),
        market: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> UniverseResult:
        if market not in (None, "All", "S&P 500", "FTSE 100"):
            raise HTTPException(status_code=422, detail="Unsupported market")
        return services.constituents.search(q, market, limit)

    @app.post("/api/v1/analyze", response_model=AnalysisResult)
    def analyze(request: AnalysisRequest) -> AnalysisResult:
        try:
            return services.analysis.analyze(request.symbol.strip().upper())
        except ConstituentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ForecastError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SentimentModelError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Unexpected analysis failure")
            raise HTTPException(status_code=500, detail="Unexpected analysis failure") from exc

    @app.get("/api/v1/companies/{symbol}/overview", response_model=CompanyOverview)
    def company_overview(symbol: str = Path(min_length=1, max_length=20)) -> CompanyOverview:
        """Read one company's Company Overview from stored data.

        A GET because it is genuinely a read: no news is fetched, no sentiment is scored, no
        article analysis is run, and no row is written. It is safe to call on every page load,
        unlike ``/api/v1/analyze``, which refreshes coverage and may spend on new analyses.
        """

        try:
            return services.analysis.read_stored(symbol.strip().upper())
        except ConstituentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Unexpected overview failure")
            raise HTTPException(status_code=500, detail="Unexpected overview failure") from exc

    @app.post("/api/v1/articles/analyze", response_model=ArticleAnalysisResponse)
    def analyze_article(request: ArticleAnalysisRequest) -> ArticleAnalysisResponse:
        """Generate one deliberate, cached event analysis for a genuine stored article."""

        return services.article_events.analyze_article(request.article_id)

    return app


app = create_app()
