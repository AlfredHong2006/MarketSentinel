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
from marketsentinel.constituents import CacheOnlyConstituentResolver, WikipediaConstituentService
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
from marketsentinel.timeutils import ensure_utc

HORIZON_DAYS_PER_MONTH = 30


def horizon_days_for(months: int) -> int:
    """Convert the CLI's month count into a horizon in days.

    Shared by every mode so a repair run replans the exact buckets the run it repairs used.
    """

    return months * HORIZON_DAYS_PER_MONTH


def parse_as_of(value: str) -> datetime:
    """Parse a pinned ``--as-of`` timestamp, requiring an explicit UTC offset.

    A naive value is rejected rather than assumed to be UTC: this argument exists to reproduce
    one historical run's exact bucket boundaries, and silently shifting it by the operator's
    local offset would replan different buckets while appearing to succeed.
    """

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("--as-of must include a UTC offset, for example 2026-08-21T18:32:05+00:00")
    return ensure_utc(parsed)


def _parse_as_of(parser: argparse.ArgumentParser, value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_as_of(value)
    except ValueError as error:
        parser.error(str(error))


def build_backfill_service(
    settings: Settings,
    *,
    bucket_candidate_cap: int,
    max_new_analyses: int,
    offline: bool = False,
) -> HistoricalIntelligenceBackfillService:
    """Wire the same dependency shapes as api/app.py::build_services, for the backfill class."""

    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    constituent_service = WikipediaConstituentService(
        cache_path=settings.constituent_cache_path,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )
    # An offline mode must not reach Wikipedia when the cache has aged past its refresh interval.
    constituents = (
        CacheOnlyConstituentResolver(constituent_service) if offline else constituent_service
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
        choices=["backfill", "reanalyze-stale", "refresh-evidence", "fill-selection-gaps"],
        default="backfill",
        help="'backfill' fetches/analyzes historical months; 'reanalyze-stale' re-runs the "
        "current Stage A/B/C version only over already-stored articles whose only analyses are "
        "version-incompatible; 'refresh-evidence' re-runs analyze_article for every currently "
        "display-compatible analysis after an evidence-selection algorithm change, relying on "
        "its existing evidence_fingerprint cache check (unchanged evidence costs nothing); "
        "'fill-selection-gaps' re-runs candidate selection over already-stored articles only -- "
        "no fetch, no scoring, no sentiment rebuild -- and analyzes just the newly selected "
        "articles, to repair a corpus built with an older selector.",
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="ISO-8601 timestamp with a UTC offset, used as 'now' when planning buckets "
        "(fill-selection-gaps only). Pin this to the original run's timestamp so the repair "
        "replans that run's exact buckets. This freezes bucket geometry and publication-time "
        "filtering only -- candidate membership is whatever the articles table holds at run "
        "time, so verify the stored article count before a repair. Defaults to the current time.",
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

    if arguments.as_of is not None and arguments.mode != "fill-selection-gaps":
        parser.error("--as-of only applies to --mode fill-selection-gaps")
    as_of = _parse_as_of(parser, arguments.as_of)

    settings = get_settings()
    service = build_backfill_service(
        settings,
        bucket_candidate_cap=arguments.bucket_candidate_cap,
        max_new_analyses=arguments.max_new_analyses,
        offline=arguments.mode == "fill-selection-gaps",
    )
    now = datetime.now(UTC)

    if arguments.mode == "reanalyze-stale":
        report = service.reanalyze_stale(arguments.ticker, now=now)
    elif arguments.mode == "refresh-evidence":
        report = service.refresh_evidence(arguments.ticker, now=now)
    elif arguments.mode == "fill-selection-gaps":
        report = service.fill_selection_gaps(
            arguments.ticker,
            now=as_of or now,
            horizon_days=horizon_days_for(arguments.months),
        )
    else:
        report = service.backfill(
            arguments.ticker, now=now, horizon_days=horizon_days_for(arguments.months)
        )

    print(report.render())


if __name__ == "__main__":
    main()
