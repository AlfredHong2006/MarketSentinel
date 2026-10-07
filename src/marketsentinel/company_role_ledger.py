"""The company-role stage under the job ledger: leases, retries, skip rule, ordering, budgets.

The role stage reuses the existing ``article_analysis_jobs`` ledger unchanged. Its jobs live under
their own ``analysis_contract`` key (``role:m=...;p=...;s=...``), so one article is paid for once
per role contract, a lease keeps two processes from paying for the same article, and failures retry
by the same rules as Stage A/B/C (``failure_transition``). Nothing about the Stage A/B/C ledger
rows, states, or summary changes: those queries are all keyed by the Stage A/B/C contract.

What is deliberately different from the Stage A/B/C runner:

- The skip rule is smaller. Only a demo article is skipped. Stage A's other per-article rules
  (low relevance, market-reaction-only, external holding, ...) decide whether an article is worth a
  *business-event* extraction; a role label is wanted for every genuine article, because an
  article skipped here would stay unlabelled and be treated by the engine's unlabelled policy.
- Ordering puts the articles that can change an `mr-v1` result first: those in a session with at
  least three distinct sources. Everything else follows, newest first. Deterministic throughout.
- "New" versus "backfill" is decided from the article alone (published inside the ticker's live
  window or not), so it needs no stored marker.

Budgets are explicit and default to zero. A run that is not given a cap does nothing here, and
budget-limited work stays ``pending``; it is never marked done.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from marketsentinel.analysis_ledger import LEASE_DURATION, failure_transition
from marketsentinel.company_role import CompanyRoleService
from marketsentinel.domain import Article, CompanyRoleResponse, ScoredArticle
from marketsentinel.market_reaction.calendars import build_session_calendar, resolve_exchange
from marketsentinel.market_reaction.models import MIN_DISTINCT_SOURCES
from marketsentinel.market_reaction.signal import build_session_signals, from_scored_article
from marketsentinel.storage.sqlite import (
    JOB_TERMINAL_STATES,
    NewAnalysisJob,
    SQLiteRepository,
)
from marketsentinel.timeutils import utc_now

# Calendar bounds around the stored articles, wide enough that the first and last article always
# fall inside the exchange calendar (same margins the engine uses).
_CALENDAR_LEAD_DAYS = 14
_CALENDAR_TAIL_DAYS = 35


@dataclass(frozen=True)
class RoleBudget:
    """Fixed, explicit spend caps for the role stage. Every default is zero.

    ``max_new_per_ticker`` and ``max_new_total`` bound paid attempts on *new* articles (published
    inside the ticker's live window) in one run. ``max_backfill_total`` bounds paid attempts on
    older stored articles across all tickers in one run; the backfill is a one-off operator
    action, so it is a per-invocation ceiling, and an article is only ever paid for once per
    contract regardless. With every cap at zero the stage is inert: it creates no job and makes no
    call.
    """

    max_new_per_ticker: int = 0
    max_new_total: int = 0
    max_backfill_total: int = 0

    def __post_init__(self) -> None:
        for name in ("max_new_per_ticker", "max_new_total", "max_backfill_total"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

    @property
    def enabled(self) -> bool:
        return self.max_backfill_total > 0 or (
            self.max_new_per_ticker > 0 and self.max_new_total > 0
        )


@dataclass(frozen=True)
class RoleLedgerOutcome:
    """What one runner call did, for budget and circuit-breaker accounting."""

    response: CompanyRoleResponse
    paid_attempt: bool
    reused: bool = False
    refused: bool = False
    job_state: str | None = None

    @property
    def stops_run(self) -> bool:
        return self.response.status == "unavailable"


class LedgeredCompanyRoleRunner:
    """Routes every role-extraction call through the job ledger."""

    def __init__(
        self,
        repository: SQLiteRepository,
        service: CompanyRoleService,
        *,
        owner_prefix: str = "role-runner",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.service = service
        self.owner_prefix = owner_prefix
        self.clock = clock

    @property
    def contract_key(self) -> str:
        return self.service.contract.contract_key

    def process(self, article_id: str, now: datetime | None = None) -> RoleLedgerOutcome:
        now = now or self.clock()
        contract = self.contract_key
        article = self.repository.get_article(article_id)
        if article is None:
            return RoleLedgerOutcome(
                CompanyRoleResponse(
                    article_id=article_id,
                    status="not_found",
                    message="The requested stored article was not found.",
                    failure_category="not_found",
                ),
                paid_attempt=False,
            )
        self.repository.insert_analysis_jobs(
            [
                NewAnalysisJob(
                    article_fingerprint=article.fingerprint,
                    analysis_contract=contract,
                    ticker=article.ticker,
                    published_at=article.published_at,
                    state="pending",
                    reason="on_demand",
                    created_at=now,
                )
            ]
        )
        owner = f"{self.owner_prefix}-{uuid4().hex}"
        claimed = self.repository.claim_analysis_job(
            article.fingerprint,
            contract,
            owner=owner,
            now=now,
            lease_expires_at=now + LEASE_DURATION,
        )
        if claimed is None:
            return self._refusal(article_id, contract)

        contract_parts = self.service.contract
        existing = self.repository.get_company_role(
            article.fingerprint,
            contract_parts.model_version,
            contract_parts.prompt_version,
            contract_parts.schema_version,
        )
        if existing is not None:
            # A label is already stored (for example the process stored it and then died before
            # the job moved): complete the job for free instead of paying again.
            self.repository.finish_analysis_job(
                article.fingerprint,
                contract,
                owner=owner,
                now=now,
                state="analyzed",
                reason="preexisting",
                paid_attempt=False,
                input_tokens=0,
                output_tokens=0,
            )
            return RoleLedgerOutcome(
                CompanyRoleResponse(article_id=article_id, status="cached", label=existing),
                paid_attempt=False,
                reused=True,
                job_state="analyzed",
            )

        usage = getattr(self.service.provider, "last_usage", None)
        if isinstance(usage, dict):
            usage.clear()
        response = self.service.label_article(article_id)
        input_tokens, output_tokens = _usage_totals(usage if isinstance(usage, dict) else None)
        return self._record(
            claimed.attempts,
            article.fingerprint,
            contract,
            owner,
            response,
            input_tokens,
            output_tokens,
            now,
        )

    def _record(
        self,
        prior_attempts: int,
        fingerprint: str,
        contract: str,
        owner: str,
        response: CompanyRoleResponse,
        input_tokens: int,
        output_tokens: int,
        now: datetime,
    ) -> RoleLedgerOutcome:
        common = {
            "owner": owner,
            "now": now,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        if response.status in ("generated", "cached"):
            self.repository.finish_analysis_job(
                fingerprint,
                contract,
                state="analyzed",
                reason=response.status,
                paid_attempt=response.status == "generated",
                **common,
            )
            return RoleLedgerOutcome(
                response, paid_attempt=response.status == "generated", job_state="analyzed"
            )
        if response.status == "unavailable":
            # Not the article's fault and nothing was spent: the job goes back to pending.
            self.repository.finish_analysis_job(
                fingerprint,
                contract,
                state="pending",
                reason="provider_unavailable",
                paid_attempt=False,
                **common,
            )
            return RoleLedgerOutcome(response, paid_attempt=False, job_state="pending")

        category = response.failure_category or (
            "not_found" if response.status == "not_found" else "unexpected"
        )
        if category == "demo":
            state, next_attempt_at, paid = "skipped", None, False
        elif category == "not_found":
            state, next_attempt_at, paid = "failed", None, False
        else:
            paid = True
            state, next_attempt_at = failure_transition(category, prior_attempts + 1, now)
        self.repository.finish_analysis_job(
            fingerprint,
            contract,
            state=state,
            reason=category,
            paid_attempt=paid,
            failure_category=None if state == "skipped" else category,
            next_attempt_at=next_attempt_at,
            **common,
        )
        return RoleLedgerOutcome(response, paid_attempt=paid, job_state=state)

    def _refusal(self, article_id: str, contract: str) -> RoleLedgerOutcome:
        job = self.repository.get_analysis_job(article_id, contract)
        state = job.state if job is not None else "unknown"
        if state == "leased":
            message = "This article is already being labelled by another process."
        elif state == "retry_wait" and job is not None and job.next_attempt_at is not None:
            message = f"A retry is already scheduled for {job.next_attempt_at.isoformat()}."
        elif state in JOB_TERMINAL_STATES:
            message = f"The role job for this article is terminal ({state})."
        else:
            message = f"The role job for this article is not claimable ({state})."
        return RoleLedgerOutcome(
            CompanyRoleResponse(
                article_id=article_id,
                status="failed",
                message=message,
                failure_category=f"ledger_{state}",
            ),
            paid_attempt=False,
            refused=True,
            job_state=state,
        )


def _usage_totals(usage: dict | None) -> tuple[int, int]:
    if not usage:
        return 0, 0
    input_tokens = output_tokens = 0
    for value in usage.values():
        if isinstance(value, tuple) and len(value) == 2:
            input_tokens += int(value[0] or 0)
            output_tokens += int(value[1] or 0)
    return input_tokens, output_tokens


def is_new_article(article: Article, now: datetime, live_window_days: int) -> bool:
    """New work: published inside the live window. Everything older is backfill."""

    return article.published_at >= now - timedelta(days=live_window_days)


def priority_article_ids(scored: Sequence[ScoredArticle], market: str | None) -> frozenset[str]:
    """Articles that can change an `mr-v1` result: those in a session with >= 3 distinct sources.

    Built with the engine's own session assignment and deduplication (``build_session_signals``),
    so "can change a result" means exactly what the engine will compute. An unresolved listing
    exchange yields no priority set rather than a guess; ordering then falls back to recency.
    """

    exchange = resolve_exchange(market)
    real = [item for item in scored if not item.is_demo]
    if exchange is None or not real:
        return frozenset()
    dates = [item.published_at.date() for item in real]
    calendar = build_session_calendar(
        exchange,
        min(dates) - timedelta(days=_CALENDAR_LEAD_DAYS),
        max(dates) + timedelta(days=_CALENDAR_TAIL_DAYS),
    )
    ticker = real[0].ticker
    built = build_session_signals([from_scored_article(item) for item in real], ticker, calendar)
    return frozenset(
        article_id
        for signal in built.signals
        if signal.distinct_source_count >= MIN_DISTINCT_SOURCES
        for article_id in signal.article_ids
    )


def role_processing_order(articles: Sequence[Article], priority: frozenset[str]) -> list[Article]:
    """Deterministic order: priority articles first, then newest first, ties by fingerprint."""

    return sorted(
        articles,
        key=lambda article: (
            article.fingerprint not in priority,
            -article.published_at.timestamp(),
            article.fingerprint,
        ),
    )


def reconcile_role_jobs(
    repository: SQLiteRepository,
    contract: str,
    ticker: str,
    now: datetime,
) -> dict[str, int]:
    """Give every stored article of the ticker exactly one role job under ``contract``.

    Spends nothing. A demo article is ``skipped``; every other article is ``pending``. An
    existing job is never replaced (``ON CONFLICT DO NOTHING``), so this is idempotent.
    """

    missing = repository.articles_without_analysis_job(ticker, contract)
    jobs = [
        NewAnalysisJob(
            article_fingerprint=article.fingerprint,
            analysis_contract=contract,
            ticker=ticker,
            published_at=article.published_at,
            state="skipped" if article.is_demo else "pending",
            reason="demo" if article.is_demo else "eligible",
            created_at=now,
        )
        for article in missing
    ]
    repository.insert_analysis_jobs(jobs)
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.state] = counts.get(job.state, 0) + 1
    return counts
