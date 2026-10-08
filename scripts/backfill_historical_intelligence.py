"""Manually-triggered historical intelligence backfill / one-time stale-version catch-up.

This performs real work against the configured database: fetching historical news, FinBERT-
scoring it, and -- unless the LLM provider is unconfigured -- making real, budget-bounded LLM
calls through the same Stage A/B/C pipeline the live app uses. It is deliberately a plain,
synchronous, one-shot script: no scheduler, queue, or background worker.

Read the printed report before assuming a run covered what you expected -- a partial/failed
month is reported explicitly per bucket, never silently presented as complete.

WARNING -- do not run this concurrently with scripts/run_coverage_cycle.py (or a private
POST /api/v1/analyze) for the same ticker. Every mode here calls the analysis service directly and
takes no analysis-job ledger lease, so a concurrent coverage cycle can pay for the same article
twice. Run backfill and repair modes only while continuous coverage for that ticker is idle.

Usage:
    python scripts/backfill_historical_intelligence.py --ticker NVDA --months 12
    python scripts/backfill_historical_intelligence.py --ticker NVDA --mode reanalyze-stale
    python scripts/backfill_historical_intelligence.py --ticker NVDA --months 36 \\
        --skip-recent-months 12 --google-only --max-new-analyses 0
    # Extend stored history backwards, ending exactly where it starts (plan first, then run):
    python scripts/backfill_historical_intelligence.py --ticker NVDA --months 36 \\
        --until-stored-start --plan-only
    python scripts/backfill_historical_intelligence.py --ticker NVDA --months 36 \\
        --until-stored-start --google-only --max-new-analyses 0 --request-interval-seconds 5.25

Exit codes: 0 done; 2 refused before any fetch (bad arguments, no stored history, boundary
outside the horizon, or the stored corpus already fills the scored-read cap); 3 stopped because
storing pushed the scored-article count past the read cap (see the message for the database state).
"""

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.backfill_service import (
    HistoricalIntelligenceBackfillService,
    ScoredReadCapExceeded,
    plan_anchored_backfill,
    resolve_anchored_boundary,
)
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
from marketsentinel.historical_backfill import BackfillRefusal
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


def offset_days_for(skip_months: int, months: int) -> int:
    """Convert ``--skip-recent-months`` into days with the same 30-day month as the horizon.

    Rejected here, with the CLI's own words, when it is negative or leaves no range, so the
    operator sees which two options disagree instead of the planner's day counts.
    """

    if skip_months < 0:
        raise ValueError("--skip-recent-months must not be negative")
    if skip_months >= months:
        raise ValueError(
            f"--skip-recent-months ({skip_months}) must be smaller than --months ({months}); "
            "otherwise no month is left to backfill"
        )
    return horizon_days_for(skip_months)


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


def parse_until(value: str) -> datetime:
    """Parse ``--until``, requiring an explicit UTC offset (a naive value is never assumed UTC)."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError("--until must include a UTC offset, for example 2025-08-27T00:00:00+00:00")
    return ensure_utc(parsed)


_SINGLE_TICKER = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{0,9}")


def is_single_ticker(value: str) -> bool:
    """True for exactly one ticker symbol: no comma, space or list."""

    return _SINGLE_TICKER.fullmatch(value.strip()) is not None


class ReadOnlySQLiteRepository(SQLiteRepository):
    """A repository whose connections cannot write, and which never creates or migrates a file.

    Plan-only mode reads through this instead of ``initialize()``, which runs the schema script
    (journal and ``user_version`` pragmas, ``CREATE``/``ALTER``) and would create a missing file.
    ``immutable=1`` keeps even the ``-wal``/``-shm`` side files from being created next to a
    WAL-mode database, which a plain ``mode=ro`` open does. The cost: an unmerged ``-wal`` is not
    read, so run it on a database no writer has open (the workflow's downloaded copy is one).
    """

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro&immutable=1", uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        return connection


def refuse(message: str) -> None:
    """Exit 2 with a message: used only before anything has been fetched or written."""

    print(f"REFUSED: {message}", file=sys.stderr)
    sys.exit(2)


def report_read_cap_exceeded(error: ScoredReadCapExceeded) -> None:
    """Exit 3 with a prominent description of the database state and what to do next."""

    bar = "=" * 78
    print(
        f"\n{bar}\nBACKFILL STOPPED: SCORED-READ CAP EXCEEDED ({error.cap})\n{bar}\n"
        f"{error}\n\n"
        "What to do next:\n"
        "  - Do not re-run. The next run refuses to start while the ticker's scored articles "
        f"reach {error.cap}.\n"
        "  - In the GitHub workflow this step fails, so the job never reaches a checkpoint: the "
        "database in R2 is unchanged and nothing needs undoing.\n"
        "  - On a local database, the rows described above stay stored and are harmless.\n"
        "  - Moving the cap is a product-owner decision (backfill_service."
        "_SENTIMENT_AGGREGATION_LIMIT); see docs/research/MR-009-proposal.md.\n"
        f"{bar}",
        file=sys.stderr,
    )
    sys.exit(3)


def concurrency_warning(ticker: str, *, under_continuous_coverage: bool) -> str:
    """The runtime warning printed before any backfill or repair mode does work."""

    warning = (
        f"WARNING: historical backfill/repair for {ticker} takes no analysis-job ledger lease. "
        "Do not run it while a coverage cycle (scripts/run_coverage_cycle.py) or a private "
        "/api/v1/analyze refresh is running for the same ticker, or one article can be paid "
        "for twice."
    )
    if under_continuous_coverage:
        warning += (
            f" {ticker} is under continuous coverage: make sure no coverage cycle is running now."
        )
    return warning


def build_backfill_service(
    settings: Settings,
    *,
    bucket_candidate_cap: int,
    max_new_analyses: int,
    offline: bool = False,
    priority_bonus_limit: int = 0,
    google_only: bool = False,
    request_interval_seconds: float = 0.0,
) -> HistoricalIntelligenceBackfillService:
    """Wire the same dependency shapes as api/app.py::build_services, for the backfill class.

    ``google_only`` makes Google News RSS the only history source, with no GDELT attempt, so a run
    cannot mix a second source into the stored sampling regime. ``max_new_analyses == 0`` wires
    the unavailable provider even when a key is configured, so a fetch-and-score run cannot reach
    a paid analysis call by any path.
    """

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
    google = GoogleNewsHistoricalProvider(
        timeout_seconds=settings.request_timeout_seconds,
        user_agent=settings.user_agent,
        request_interval_seconds=request_interval_seconds,
    )
    historical_news = (
        HistoricalNewsService(primary=google)
        if google_only
        else HistoricalNewsService(
            primary=GdeltHistoricalNewsProvider(
                timeout_seconds=settings.request_timeout_seconds,
                user_agent=settings.user_agent,
                window_days=settings.historical_gdelt_window_days,
                request_interval_seconds=settings.historical_gdelt_request_interval_seconds,
            ),
            rss_fallback=google,
        )
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
        if settings.llm_api_key and max_new_analyses > 0
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
        priority_bonus_limit=priority_bonus_limit,
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
    boundary = parser.add_mutually_exclusive_group()
    boundary.add_argument(
        "--skip-recent-months",
        type=int,
        default=None,
        help="Backfill mode only. Skip the most recent N 30-day months of the horizon and fetch "
        "nothing for them (default: 0, i.e. the whole horizon). '--months 36 --skip-recent-months "
        "12' plans months 13-36 counted back from today, on the same calendar-month buckets a "
        "plain '--months 36' run would use.",
    )
    boundary.add_argument(
        "--until-stored-start",
        action="store_true",
        help="Backfill mode only. End the range exactly at the earliest stored published_at of "
        "this ticker's non-demo articles, read at run time from the database the run uses. The "
        "bucket containing it is clipped there; every earlier bucket boundary is a plain "
        "--months run's. Nothing at or after it is stored.",
    )
    boundary.add_argument(
        "--until",
        default=None,
        metavar="TIMESTAMP",
        help="Backfill mode only. Like --until-stored-start but with an explicit end: an "
        "ISO-8601 timestamp with a UTC offset, for example 2025-08-27T00:00:00+00:00.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="With --until-stored-start or --until. Resolve and print the boundary, the five "
        "earliest stored published_at values, the stored count for the 30 days after the "
        "boundary and the planned buckets, then exit: no network call, no database write.",
    )
    parser.add_argument(
        "--google-only",
        action="store_true",
        help="Backfill mode only. Use Google News RSS as the sole history source; GDELT is never "
        "contacted, so no second source can enter the stored sampling regime.",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=0.0,
        help="Minimum seconds between Google News RSS requests, redirect resolutions included "
        "(default: 0, unpaced as before).",
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
        "--priority-bonus",
        type=int,
        default=0,
        help="Extra candidates a bucket may add beyond --bucket-candidate-cap, drawn only from "
        "articles reporting a financial disclosure or a concrete corporate action (default: 0, "
        "i.e. a fixed budget). Use with a lower base cap so a quiet month costs less and a month "
        "with real activity can afford more.",
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
    anchored = arguments.until_stored_start or arguments.until is not None
    if arguments.mode != "backfill" and (
        arguments.skip_recent_months is not None
        or anchored
        or arguments.plan_only
        or arguments.google_only
        or arguments.request_interval_seconds
    ):
        parser.error(
            "--skip-recent-months, --until-stored-start, --until, --plan-only, --google-only and "
            "--request-interval-seconds only apply to --mode backfill"
        )
    if arguments.plan_only and not anchored:
        parser.error("--plan-only needs --until-stored-start or --until")
    if not is_single_ticker(arguments.ticker):
        parser.error("--ticker takes exactly one ticker symbol (no commas or spaces)")
    if arguments.request_interval_seconds < 0:
        parser.error("--request-interval-seconds must not be negative")
    until_override: datetime | None = None
    if arguments.until is not None:
        try:
            until_override = parse_until(arguments.until)
        except ValueError as error:
            parser.error(str(error))
    offset_days = 0
    if arguments.skip_recent_months:
        try:
            offset_days = offset_days_for(arguments.skip_recent_months, arguments.months)
        except ValueError as error:
            parser.error(str(error))

    settings = get_settings()
    ticker = arguments.ticker.strip().upper()
    horizon_days = horizon_days_for(arguments.months)
    if anchored and not settings.database_path.exists():
        # Without this, opening the repository would create an empty database and the run would
        # then "refuse" on it; say what is actually wrong instead, and leave no file behind.
        refuse(f"no database at {settings.database_path}; there is no stored history to extend.")
    if arguments.plan_only:
        try:
            plan = plan_anchored_backfill(
                ReadOnlySQLiteRepository(settings.database_path),
                ticker,
                now=datetime.now(UTC),
                horizon_days=horizon_days,
                override=until_override,
            )
        except BackfillRefusal as error:
            refuse(str(error))
        print(plan.render())
        return

    service = build_backfill_service(
        settings,
        bucket_candidate_cap=arguments.bucket_candidate_cap,
        max_new_analyses=arguments.max_new_analyses,
        offline=arguments.mode == "fill-selection-gaps",
        priority_bonus_limit=arguments.priority_bonus,
        google_only=arguments.google_only,
        request_interval_seconds=arguments.request_interval_seconds,
    )
    coverage = service.repository.get_company_coverage(arguments.ticker.strip().upper())
    print(
        concurrency_warning(
            arguments.ticker,
            under_continuous_coverage=coverage is not None and coverage.active,
        ),
        file=sys.stderr,
    )
    now = datetime.now(UTC)

    # Anchored runs read their end from the database before the first fetch, and refuse here.
    until_arguments: dict[str, datetime] = {}
    if anchored:
        try:
            until = resolve_anchored_boundary(
                service.repository,
                ticker,
                now=now,
                horizon_days=horizon_days,
                override=until_override,
            )
        except BackfillRefusal as error:
            refuse(str(error))
        print(f"Boundary: nothing at or after {until.isoformat()} will be stored.")
        until_arguments = {"until": until}

    try:
        if arguments.mode == "reanalyze-stale":
            report = service.reanalyze_stale(arguments.ticker, now=now)
        elif arguments.mode == "refresh-evidence":
            report = service.refresh_evidence(arguments.ticker, now=now)
        elif arguments.mode == "fill-selection-gaps":
            report = service.fill_selection_gaps(
                arguments.ticker, now=as_of or now, horizon_days=horizon_days
            )
        else:
            report = service.backfill(
                arguments.ticker,
                now=now,
                horizon_days=horizon_days,
                offset_days=offset_days,
                **until_arguments,
            )
    except BackfillRefusal as error:
        refuse(str(error))
    except ScoredReadCapExceeded as error:
        report_read_cap_exceeded(error)

    print(report.render())


if __name__ == "__main__":
    main()
