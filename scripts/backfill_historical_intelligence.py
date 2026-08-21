"""Manually-triggered historical intelligence backfill / one-time stale-version catch-up.

This performs real work against the configured database: fetching historical news, FinBERT-
scoring it, and -- unless the LLM provider is unconfigured -- making real, budget-bounded LLM
calls through the same Stage A/B/C pipeline the live app uses. It is deliberately a plain,
synchronous, one-shot script: no scheduler, queue, or background worker.

Read the printed report before assuming a run covered what you expected -- a partial/failed
month is reported explicitly per bucket, never silently presented as complete.

Usage:
    python scripts/backfill_historical_intelligence.py --ticker NVDA --months 12
    python scripts/backfill_historical_intelligence.py --ticker NVDA --mode reanalyze-stale
"""

import argparse
from datetime import UTC, datetime

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.backfill_service import HistoricalIntelligenceBackfillService
from marketsentinel.config import Settings, get_settings
from marketsentinel.constituents import WikipediaConstituentService
from marketsentinel.event_analysis import (
    ARTICLE_ANALYSIS_SCHEMA_VERSION,
    STAGE_A_PROMPT_VERSION,
    STAGE_B_PROMPT_VERSION,
    STAGE_C_PROMPT_VERSION,
    ArticleEventAnalysisService,
    OpenAIArticleIntelligenceProvider,
    UnavailableArticleAnalysisProvider,
)
from marketsentinel.sentiment.finbert import FinBertAnalyzer
from marketsentinel.sources.historical import (
    GdeltHistoricalNewsProvider,
    GoogleNewsHistoricalProvider,
    HistoricalNewsService,
)
from marketsentinel.storage.sqlite import SQLiteRepository


def build_backfill_service(
    settings: Settings, *, bucket_candidate_cap: int, max_new_analyses: int
) -> HistoricalIntelligenceBackfillService:
    """Wire the same dependency shapes as api/app.py::build_services, for the backfill class."""

    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    constituents = WikipediaConstituentService(
        cache_path=settings.constituent_cache_path,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )
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
    compatibility = ArticleAnalysisCompatibility(
        model_version=settings.llm_model,
        stage_a_prompt_version=STAGE_A_PROMPT_VERSION,
        stage_b_prompt_version=STAGE_B_PROMPT_VERSION,
        stage_c_prompt_version=STAGE_C_PROMPT_VERSION,
        schema_version=ARTICLE_ANALYSIS_SCHEMA_VERSION,
    )
    return HistoricalIntelligenceBackfillService(
        constituents=constituents,
        historical_news=historical_news,
        sentiment=sentiment,
        repository=repository,
        article_analysis_runner=article_events,
        article_analysis_compatibility=compatibility,
        bucket_candidate_cap=bucket_candidate_cap,
        max_new_analyses_per_run=max_new_analyses,
        sentiment_half_life_hours=settings.sentiment_half_life_hours,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument(
        "--months",
        type=int,
        default=12,
        help="Backfill horizon in 30-day months (default: 12, i.e. a 360-day horizon).",
    )
    parser.add_argument(
        "--mode",
        choices=["backfill", "reanalyze-stale", "refresh-evidence"],
        default="backfill",
        help="'backfill' fetches/analyzes historical months; 'reanalyze-stale' re-runs the "
        "current Stage A/B/C version only over already-stored articles whose only analyses are "
        "version-incompatible; 'refresh-evidence' re-runs analyze_article for every currently "
        "display-compatible analysis after an evidence-selection algorithm change, relying on "
        "its existing evidence_fingerprint cache check (unchanged evidence costs nothing).",
    )
    parser.add_argument(
        "--bucket-candidate-cap",
        type=int,
        default=5,
        help="Max analysis candidates selected per calendar-month bucket (default: 5 -- a "
        "conservative starting point for a first validation run, not 6).",
    )
    parser.add_argument(
        "--max-new-analyses",
        type=int,
        default=60,
        help="Run-level cap on new LLM analysis attempts. Cached hits never count against this.",
    )
    arguments = parser.parse_args()

    settings = get_settings()
    service = build_backfill_service(
        settings,
        bucket_candidate_cap=arguments.bucket_candidate_cap,
        max_new_analyses=arguments.max_new_analyses,
    )
    now = datetime.now(UTC)

    if arguments.mode == "reanalyze-stale":
        report = service.reanalyze_stale(arguments.ticker, now=now)
    elif arguments.mode == "refresh-evidence":
        report = service.refresh_evidence(arguments.ticker, now=now)
    else:
        report = service.backfill(arguments.ticker, now=now, horizon_days=arguments.months * 30)

    print(report.render())


if __name__ == "__main__":
    main()
