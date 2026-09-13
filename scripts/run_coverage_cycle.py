"""Continuous coverage for actively covered companies: activate, run one cycle, or report status.

``--mode activate`` registers tickers and gives every stored article one explicit ledger state. It
fetches nothing and makes no LLM call, and it resolves companies from the local constituent cache
only.

``--mode cycle`` performs real work against the configured database: it fetches news from each
provider since that provider's own watermark (minus an overlap window), stores and FinBERT-scores
new articles, reconciles them into the ledger, and -- unless the LLM provider is unconfigured --
makes real, budget-bounded Stage A/B/C calls for pending articles through the job ledger. Budget-
limited work stays pending for the next cycle.

``--mode status`` prints the ledger and watermark state without fetching or analysing.

Synchronous and one-shot: no scheduler, queue, or background worker. Schedule it externally (for
example Windows Task Scheduler) if it should repeat.

WARNING -- do not run a cycle concurrently with scripts/backfill_historical_intelligence.py (any
mode, including refresh-evidence and reanalyze-stale) for the same ticker. The backfill and repair
path takes no analysis-job ledger lease, so the two can pay for the same article twice.

A change to the model, a prompt version, or the schema version is a new analysis contract. The
next reconcile gives pending jobs only to articles published on or after the ticker's original
live-window start (ledger_started_at - live window); stored history older than that becomes
baseline under the new contract and is NOT re-analysed automatically. Re-analysing that history
still requires an explicit backfill mode such as reanalyze-stale.

Usage:
    python scripts/run_coverage_cycle.py --mode activate --ticker NVDA --ticker PFE
    python scripts/run_coverage_cycle.py --ticker NVDA --ticker PFE --max-new 10
    python scripts/run_coverage_cycle.py --mode status --ticker NVDA --ticker PFE
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.analysis_ledger import LedgeredArticleAnalysisRunner
from marketsentinel.config import Settings, get_settings
from marketsentinel.constituents import CacheOnlyConstituentResolver, WikipediaConstituentService
from marketsentinel.coverage_cycle import (
    CoverageCycleService,
    HistoricalProviderSource,
    RecentProviderSource,
)
from marketsentinel.errors import CoverageNotActiveError
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
from marketsentinel.sources.historical import GdeltHistoricalNewsProvider
from marketsentinel.sources.news import GoogleNewsRssProvider
from marketsentinel.storage.sqlite import SQLiteRepository

GDELT_SOURCE = "gdelt"
GOOGLE_NEWS_RSS_SOURCE = "google_news_rss"


def build_coverage_service(settings: Settings, *, offline: bool) -> CoverageCycleService:
    """Wire the same dependency shapes as api/app.py::build_services, for the coverage cycle."""

    repository = SQLiteRepository(settings.database_path)
    repository.initialize()
    constituent_service = WikipediaConstituentService(
        cache_path=settings.constituent_cache_path,
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
    )
    constituents = (
        CacheOnlyConstituentResolver(constituent_service) if offline else constituent_service
    )
    # No demo fallback and no GDELT-to-RSS fallback: each source is a real provider under its own
    # watermark, so one provider's failure is recorded against that provider alone.
    sources = (
        HistoricalProviderSource(
            name=GDELT_SOURCE,
            provider=GdeltHistoricalNewsProvider(
                timeout_seconds=settings.request_timeout_seconds,
                user_agent=settings.user_agent,
                window_days=settings.historical_gdelt_window_days,
                request_interval_seconds=settings.historical_gdelt_request_interval_seconds,
            ),
            max_lookback=timedelta(days=settings.historical_news_days),
            max_articles=settings.historical_news_max_articles,
        ),
        RecentProviderSource(
            name=GOOGLE_NEWS_RSS_SOURCE,
            provider=GoogleNewsRssProvider(
                timeout_seconds=settings.request_timeout_seconds,
                user_agent=settings.user_agent,
            ),
            max_lookback=timedelta(days=settings.news_lookback_days),
            max_articles=settings.news_max_articles,
        ),
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
    return CoverageCycleService(
        constituents=constituents,
        sources=sources,
        sentiment=FinBertAnalyzer(
            model_name=settings.finbert_model,
            device=settings.finbert_device,
            batch_size=settings.finbert_batch_size,
            hf_token=settings.hf_token,
        ),
        repository=repository,
        runner=LedgeredArticleAnalysisRunner(
            repository, article_events, compatibility, owner_prefix="coverage-cycle"
        ),
        live_window_days=settings.historical_news_days,
        sentiment_window_days=settings.historical_news_days,
        sentiment_half_life_hours=settings.sentiment_half_life_hours,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker",
        action="append",
        required=True,
        help="Ticker to cover; repeat for several (for example --ticker NVDA --ticker PFE).",
    )
    parser.add_argument("--mode", choices=["cycle", "activate", "status"], default="cycle")
    parser.add_argument(
        "--max-new",
        type=int,
        default=10,
        help="Per-ticker cap on new paid analysis attempts in one cycle (default: 10). Reused "
        "analyses never count. Pending work beyond the cap waits for the next cycle.",
    )
    parser.add_argument(
        "--no-ingest", action="store_true", help="Skip news fetching in a cycle (no network)."
    )
    parser.add_argument(
        "--no-analyze", action="store_true", help="Skip the paid analysis pass in a cycle."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.max_new < 0:
        parser.error("--max-new must not be negative")
    if arguments.mode != "cycle" and (arguments.no_ingest or arguments.no_analyze):
        parser.error("--no-ingest and --no-analyze only apply to --mode cycle")
    tickers = list(dict.fromkeys(ticker.strip().upper() for ticker in arguments.ticker))

    service = build_coverage_service(get_settings(), offline=arguments.mode != "cycle")
    exit_code = 0
    for ticker in tickers:
        now = datetime.now(UTC)
        try:
            if arguments.mode == "activate":
                report = service.activate(ticker, now=now)
            elif arguments.mode == "status":
                report = service.status(ticker)
            else:
                report = service.run(
                    ticker,
                    now=now,
                    max_new_analyses=arguments.max_new,
                    ingest=not arguments.no_ingest,
                    analyze=not arguments.no_analyze,
                )
        except CoverageNotActiveError as error:
            print(f"{ticker}: {error}", file=sys.stderr)
            exit_code = 2
            continue
        print(report.render())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
