"""FastAPI boundary for constituent search and full MarketSentinel analysis."""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.config import Settings, get_settings
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.domain import (
    AnalysisResult,
    ArticleAnalysisResponse,
    CapabilitiesView,
    CompanyOverview,
    RelevantNewsView,
    StoredArticleAnalysisView,
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
from marketsentinel.sources.prices import CachingPriceProvider, YFinancePriceProvider
from marketsentinel.storage.sqlite import SQLiteRepository

LOGGER = logging.getLogger(__name__)

# Below roughly a kilobyte, compression costs more than it saves and can grow a tiny body, so the
# small responses (/health, /capabilities) are left alone.
_GZIP_MINIMUM_BYTES = 1000

# Starlette defaults to 9; 6 is zlib's own default. Measured on this API's largest payload (the
# 872KB NVDA article list): level 6 gives 389.5KB in 17.9ms against level 9's 386.8KB in 20.2ms --
# 0.7% larger for ~11% less CPU, spent per request on the server. Either is defensible; 6 is
# chosen because the bytes saved by 9 do not repay the CPU under concurrent public traffic.
_GZIP_COMPRESS_LEVEL = 6


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
        # Price history is the only unpersisted part of the read path, so without this wrapper
        # every visitor page load would reach yfinance. In-process and in-memory only.
        prices=CachingPriceProvider(
            YFinancePriceProvider(),
            ttl_seconds=settings.price_cache_ttl_seconds,
        ),
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
    # Transport only -- no route, contract, or payload changes, and a client that does not send
    # Accept-Encoding still receives identical bytes. It matters because the Relevant News window
    # is deliberately uncapped: one company's article list is ~870KB of highly repetitive JSON,
    # which compresses to roughly 390KB.
    #
    # Added before CORS, so CORS is the outer layer (Starlette runs the most recently added
    # first). Both orders were verified to behave identically for these requests, since CORS
    # attaches its headers either way; CORS is kept outermost as the conventional arrangement,
    # where it can still label a response produced by a failure further in.
    app.add_middleware(
        GZipMiddleware,
        minimum_size=_GZIP_MINIMUM_BYTES,
        compresslevel=_GZIP_COMPRESS_LEVEL,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # Public mode's only enforcement is refuse_when_public below, applied at the boundary and
    # never in a client -- the API is reachable independently of any frontend, so a hidden button
    # is not a restriction. Search and every read endpoint serve the full constituent universe in
    # both modes; the prepared set is a label for the deliberately backfilled deep
    # demonstrations, not a gate.
    prepared_symbols = sorted(item.strip().upper() for item in settings.public_prepared_companies)

    def refuse_when_public() -> None:
        """Close the two endpoints that spend money or mutate stored data.

        A public deployment is a read-only window onto an already-prepared corpus. These return
        404 so the surface simply does not exist rather than advertising a disabled capability.
        """

        if settings.public_mode:
            raise HTTPException(status_code=404, detail="Not Found")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "MarketSentinel"}

    @app.get("/api/v1/capabilities", response_model=CapabilitiesView)
    def capabilities() -> CapabilitiesView:
        """Describe this deployment's scope so a client renders it instead of guessing.

        Advisory only: the POST restrictions reported here are independently enforced above, so a
        client that ignores this response gains nothing. ``prepared_companies`` and ``coverage``
        are labels a client may attach to search results -- which companies were deliberately
        backfilled, and how many articles each ticker actually has stored. Neither restricts what
        may be read.
        """

        return CapabilitiesView(
            mode="public" if settings.public_mode else "private",
            default_symbol=settings.public_default_symbol.strip().upper(),
            prepared_companies=prepared_symbols,
            coverage=services.repository.stored_article_counts(),
            supports_refresh=not settings.public_mode,
            supports_article_analysis=not settings.public_mode,
        )

    @app.get("/api/v1/constituents/search", response_model=UniverseResult)
    def search_constituents(
        q: str = Query(default="", max_length=100),
        market: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> UniverseResult:
        if market not in (None, "All", "S&P 500", "FTSE 100"):
            raise HTTPException(status_code=422, detail="Unsupported market")
        # Identical in both modes: a public deployment is genuinely searchable across the full
        # constituent universe. Coverage labelling belongs to the capabilities endpoint, never to
        # filtering here.
        return services.constituents.search(q, market, limit)

    @app.post("/api/v1/analyze", response_model=AnalysisResult)
    def analyze(request: AnalysisRequest) -> AnalysisResult:
        refuse_when_public()
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

    @app.get("/api/v1/companies/{symbol}/articles", response_model=RelevantNewsView)
    def company_relevant_news(symbol: str = Path(min_length=1, max_length=20)) -> RelevantNewsView:
        """Read every stored, sentiment-scored article for one company.

        A GET, like the overview endpoint: no news is fetched, no sentiment is scored, no article
        analysis is run, and no row is written. This is the read-only Relevant News browser --
        deliberately with no path to the paid per-article analysis action.
        """

        try:
            return services.analysis.list_relevant_news(symbol.strip().upper())
        except ConstituentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Unexpected relevant news failure")
            raise HTTPException(status_code=500, detail="Unexpected relevant news failure") from exc

    @app.get(
        "/api/v1/companies/{symbol}/articles/{article_id}/analysis",
        response_model=StoredArticleAnalysisView,
    )
    def company_article_analysis(
        symbol: str = Path(min_length=1, max_length=20),
        article_id: str = Path(min_length=1, max_length=128),
    ) -> StoredArticleAnalysisView:
        """Read one already-stored article analysis.

        A GET, and strictly a read of what exists: it never generates an analysis, so unlike
        ``POST /api/v1/articles/analyze`` it costs nothing and is safe on a public deployment.
        An article with no compatible stored analysis is a 404, not an offer to create one.
        """

        try:
            found = services.analysis.read_article_analysis(symbol.strip().upper(), article_id)
        except ConstituentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("Unexpected article analysis read failure")
            raise HTTPException(
                status_code=500, detail="Unexpected article analysis read failure"
            ) from exc
        if found is None:
            raise HTTPException(
                status_code=404, detail="No compatible stored analysis for this article"
            )
        return found

    @app.post("/api/v1/articles/analyze", response_model=ArticleAnalysisResponse)
    def analyze_article(request: ArticleAnalysisRequest) -> ArticleAnalysisResponse:
        """Generate one deliberate, cached event analysis for a genuine stored article."""

        refuse_when_public()
        return services.article_events.analyze_article(request.article_id)

    # Mounted last, deliberately. Starlette matches routes in registration order, so every route
    # above -- /health, /docs, and all of /api/v1 -- still wins before the catch-all reaches the
    # static client. Serving the built client from the API process is what makes a single-service
    # deployment same-origin: the browser then issues relative requests and CORS is not involved.
    #
    # Unset by default, so a local run is unchanged and the Vite dev server keeps owning the
    # frontend. A configured directory that does not exist is a deployment mistake worth failing
    # loudly on at startup, rather than silently serving 404s for every page.
    if settings.frontend_dist_path is not None:
        dist = settings.frontend_dist_path
        if not (dist / "index.html").is_file():
            raise RuntimeError(
                f"frontend_dist_path {dist} does not contain index.html; build the client first"
            )
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app


app = create_app()
