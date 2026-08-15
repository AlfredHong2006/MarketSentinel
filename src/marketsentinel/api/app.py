"""FastAPI boundary for constituent search and full MarketSentinel analysis."""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from marketsentinel.config import Settings, get_settings
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.domain import AnalysisResult, UniverseResult
from marketsentinel.errors import (
    ConstituentNotFoundError,
    ForecastError,
    ProviderError,
    SentimentModelError,
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


@dataclass
class Services:
    repository: SQLiteRepository
    constituents: WikipediaConstituentService
    analysis: MarketAnalysisService


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
    )
    return Services(repository=repository, constituents=constituents, analysis=analysis)


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
        allow_origins=["http://localhost:8501", "http://127.0.0.1:8501"],
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

    return app


app = create_app()
