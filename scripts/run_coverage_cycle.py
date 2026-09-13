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

``--mode requests`` admits shared public requests (see marketsentinel/public_requests.py) from a
directory the workflow synced from the request bucket: it activates requested companies (spends
nothing) and analyses requested articles through the explicit-request ledger runner (real,
capped spend), then writes the exact keys it consumed to ``--consumed-keys`` so the workflow
deletes those and only those. ``--mode list-active`` prints every actively covered ticker.

``--all-active`` (cycle only) replaces ``--ticker`` with every active ticker in the ledger,
ordered never-cycled first then least recently attempted, so ``--max-tickers`` makes a capped
run round-robin. ``--max-new-total`` bounds paid attempts across all tickers in one invocation.

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
from pathlib import Path

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
from marketsentinel.public_requests import (
    DirectoryRequestSink,
    TickerCycleOrder,
    admit_public_requests,
    order_tickers_for_cycle,
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


def active_tickers_in_cycle_order(service: CoverageCycleService) -> list[str]:
    """Every active ticker, never-cycled first, then least recently attempted by any provider."""

    entries = []
    for coverage in service.repository.list_company_coverage():
        if not coverage.active:
            continue
        attempts = [
            watermark.last_attempt_at
            for watermark in service.repository.list_ingestion_watermarks(coverage.ticker)
        ]
        entries.append(
            TickerCycleOrder(
                ticker=coverage.ticker, oldest_attempt_at=min(attempts) if attempts else None
            )
        )
    return order_tickers_for_cycle(entries)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ticker",
        action="append",
        default=[],
        help="Ticker to cover; repeat for several (for example --ticker NVDA --ticker PFE).",
    )
    parser.add_argument(
        "--all-active",
        action="store_true",
        help="Cycle every actively covered ticker instead of naming them (cycle mode only).",
    )
    parser.add_argument(
        "--mode",
        choices=["cycle", "activate", "status", "requests", "list-active"],
        default="cycle",
    )
    parser.add_argument(
        "--max-new",
        type=int,
        default=10,
        help="Per-ticker cap on new paid analysis attempts in one cycle (default: 10). Reused "
        "analyses never count. Pending work beyond the cap waits for the next cycle.",
    )
    parser.add_argument(
        "--max-new-total",
        type=int,
        default=None,
        help="Hard cap on paid analysis attempts across every ticker in this invocation. "
        "Unset means only the per-ticker --max-new applies.",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=None,
        help="Cycle at most this many tickers this invocation (with --all-active this is "
        "round-robin: the tickers left out come first next time).",
    )
    parser.add_argument(
        "--no-ingest", action="store_true", help="Skip news fetching in a cycle (no network)."
    )
    parser.add_argument(
        "--no-analyze", action="store_true", help="Skip the paid analysis pass in a cycle."
    )
    parser.add_argument(
        "--requests-dir",
        type=Path,
        default=None,
        help="Directory of synced public request objects (requests mode).",
    )
    parser.add_argument(
        "--consumed-keys",
        type=Path,
        default=None,
        help="Where to write the consumed request keys, one per line (requests mode).",
    )
    parser.add_argument(
        "--max-new-tickers",
        type=int,
        default=3,
        help="Cap on companies newly activated from public requests in one run (default: 3).",
    )
    parser.add_argument(
        "--max-article-requests",
        type=int,
        default=20,
        help="Cap on public article analysis requests processed in one run (default: 20).",
    )
    return parser


def validate_arguments(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    for name in ("max_new", "max_new_total", "max_tickers", "max_new_tickers"):
        value = getattr(arguments, name)
        if value is not None and value < 0:
            parser.error(f"--{name.replace('_', '-')} must not be negative")
    if arguments.max_article_requests < 0:
        parser.error("--max-article-requests must not be negative")
    if arguments.mode != "cycle" and (arguments.no_ingest or arguments.no_analyze):
        parser.error("--no-ingest and --no-analyze only apply to --mode cycle")
    if arguments.all_active and arguments.mode != "cycle":
        parser.error("--all-active only applies to --mode cycle")
    if arguments.mode == "requests":
        if arguments.requests_dir is None or arguments.consumed_keys is None:
            parser.error("--mode requests needs --requests-dir and --consumed-keys")
    elif arguments.mode != "list-active" and not arguments.ticker and not arguments.all_active:
        parser.error("name at least one --ticker (or use --all-active with --mode cycle)")


def run_requests_mode(service: CoverageCycleService, arguments: argparse.Namespace) -> int:
    # Only an explicit request may reopen a terminal job, exactly like the private per-article
    # endpoint; the automatic cycle never does. Same repository, provider, and contract.
    runner = LedgeredArticleAnalysisRunner(
        service.repository,
        service.runner.analysis_service,
        service.runner.compatibility,
        allow_terminal_retry=True,
        owner_prefix="public-request",
    )
    report = admit_public_requests(
        sink=DirectoryRequestSink(arguments.requests_dir),
        coverage=service,
        runner=runner,
        repository=service.repository,
        now=datetime.now(UTC),
        max_new_tickers=arguments.max_new_tickers,
        max_article_requests=arguments.max_article_requests,
    )
    print(report.render())
    arguments.consumed_keys.parent.mkdir(parents=True, exist_ok=True)
    arguments.consumed_keys.write_text(
        "".join(f"{key}\n" for key in report.consumed_keys), encoding="utf-8"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    validate_arguments(parser, arguments)

    service = build_coverage_service(get_settings(), offline=arguments.mode != "cycle")
    if arguments.mode == "list-active":
        print(",".join(active_tickers_in_cycle_order(service)))
        return 0
    if arguments.mode == "requests":
        return run_requests_mode(service, arguments)

    if arguments.all_active:
        tickers = active_tickers_in_cycle_order(service)
    else:
        tickers = list(dict.fromkeys(ticker.strip().upper() for ticker in arguments.ticker))
    if arguments.max_tickers is not None:
        skipped = tickers[arguments.max_tickers :]
        tickers = tickers[: arguments.max_tickers]
        if skipped:
            print(f"ticker cap reached; left for a later run: {', '.join(skipped)}")

    remaining_total = arguments.max_new_total
    exit_code = 0
    for ticker in tickers:
        now = datetime.now(UTC)
        budget = arguments.max_new
        if remaining_total is not None:
            budget = min(budget, remaining_total)
        try:
            if arguments.mode == "activate":
                report = service.activate(ticker, now=now)
            elif arguments.mode == "status":
                report = service.status(ticker)
            else:
                report = service.run(
                    ticker,
                    now=now,
                    max_new_analyses=budget,
                    ingest=not arguments.no_ingest,
                    analyze=not arguments.no_analyze,
                )
                if remaining_total is not None:
                    remaining_total = max(0, remaining_total - report.analysis.paid_attempts)
        except CoverageNotActiveError as error:
            print(f"{ticker}: {error}", file=sys.stderr)
            exit_code = 2
            continue
        print(report.render())
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
