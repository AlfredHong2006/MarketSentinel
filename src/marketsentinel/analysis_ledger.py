"""The article-analysis job ledger: one explicit, durable state per article per analysis contract.

Every spending path that analyses articles automatically or on request goes through
``LedgeredArticleAnalysisRunner``. It records attempts, failures, and token usage; it leases a job
before a paid call so two processes cannot pay for the same article; and it reuses a stored
current-contract analysis instead of paying again when only the evidence pool has grown.

What this module deliberately does *not* do:

- It never decides materiality, grouping, ranking, or risk. Those layers stay unpersisted.
- It never ends a relevant article's life because of a budget, a cap, or processing order. Only
  deterministic per-article irrelevance rules (``deterministic_skip_reason``) terminate a job as
  ``skipped``; budget-limited work simply stays ``pending``.
- It does not change ``ArticleEventAnalysisService.analyze_article`` or its evidence-fingerprint
  cache. The explicit ``refresh-evidence`` backfill mode still regenerates changed evidence.

The pure helpers here take no clock and do no I/O; the runner receives ``now`` from its caller or
an injected clock.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from marketsentinel.analysis_candidates import select_analysis_candidates_with_diagnostics
from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.domain import Article, ArticleAnalysisResponse
from marketsentinel.event_analysis import ArticleEventAnalysisService
from marketsentinel.ownership_patterns import CompanyIdentity
from marketsentinel.storage.sqlite import (
    JOB_TERMINAL_STATES,
    NewAnalysisJob,
    SQLiteRepository,
)
from marketsentinel.timeutils import utc_now

# Three sequential stages, each bounded by the provider timeout and the SDK's own retries. Long
# enough that a slow but live attempt keeps its lease; short enough that a crashed process's job
# becomes claimable again within one scheduled cycle.
LEASE_DURATION = timedelta(minutes=10)

# The selector's candidate ceiling. Ordering uses at most this many ranked articles per pass; the
# rest follow by recency and are reached on later passes -- never dropped.
_ORDERING_RANK_LIMIT = 40


@dataclass(frozen=True)
class RetryRule:
    max_attempts: int
    base_delay: timedelta


# Transient provider trouble is retried with 15 min / 1 h / 4 h backoff. A validation rejection at
# temperature 0 is likely to repeat on the same input, so it gets one retry (the evidence pool may
# have changed) before becoming a permanent failure. Anything not listed is not retried.
_TRANSIENT = RetryRule(max_attempts=4, base_delay=timedelta(minutes=15))
_VALIDATION = RetryRule(max_attempts=2, base_delay=timedelta(minutes=15))
RETRY_RULES: dict[str, RetryRule] = {
    "timeout": _TRANSIENT,
    "transport_error": _TRANSIENT,
    "http_error": _TRANSIENT,
    "lease_expired": _TRANSIENT,
    "pydantic_validation": _VALIDATION,
    "semantic_validation": _VALIDATION,
    "unexpected": _VALIDATION,
}

# Diagnostics counters of the selector's per-article rejection rules, mapped to ledger reasons.
# Each is a property of the article alone. The publisher cap, official-company cap, near-title
# rule, and limit are relative to other articles, so they only ever affect processing order.
_SKIP_REASONS = (
    ("demo_rejected", "demo"),
    ("low_relevance_rejected", "low_relevance"),
    ("obvious_holdings_rejected", "external_holding"),
    ("excluded_prediction", "price_prediction"),
    ("market_reaction_rejected", "market_reaction_only"),
    ("scheduled_insider_sale_rejected", "scheduled_insider_sale"),
)


def deterministic_skip_reason(article: Article, subject_company: CompanyIdentity) -> str | None:
    """Return why an article is irrelevant on its own, or ``None`` when it should be analysed.

    Reuses the existing selector unchanged by selecting over this single article: with one
    article no relative cap or near-title comparison can bind, so a rejection can only come from
    a per-article rule, and the diagnostics counter names which one.
    """

    selection = select_analysis_candidates_with_diagnostics(
        [article], article.published_at, 1, subject_company=subject_company
    )
    if selection.candidates:
        return None
    for counter, reason in _SKIP_REASONS:
        if getattr(selection.diagnostics, counter):
            return reason
    return None


def processing_order(
    articles: Sequence[Article], now: datetime, subject_company: CompanyIdentity
) -> list[Article]:
    """Order claimable articles: the selector's ranked admits first, then everything else.

    The selector keeps its role of deciding what is most worth paying for *first*. Articles it
    does not admit this pass follow newest first, so a budget-limited run defers them rather than
    discarding them.
    """

    if not articles:
        return []
    ranked = select_analysis_candidates_with_diagnostics(
        articles,
        now,
        min(_ORDERING_RANK_LIMIT, len(articles)),
        subject_company=subject_company,
        prioritize_disclosures=True,
    ).candidates
    admitted = {article.fingerprint for article in ranked}
    remainder = sorted(
        (article for article in articles if article.fingerprint not in admitted),
        key=lambda article: (-article.published_at.timestamp(), article.fingerprint),
    )
    return [*ranked, *remainder]


def failure_transition(category: str, attempts: int, now: datetime) -> tuple[str, datetime | None]:
    """The state a failed attempt leads to: ``retry_wait`` with a due time, or ``failed``."""

    rule = RETRY_RULES.get(category)
    if rule is None or attempts >= rule.max_attempts:
        return "failed", None
    return "retry_wait", now + rule.base_delay * (4 ** max(attempts - 1, 0))


@dataclass(frozen=True)
class LedgerOutcome:
    """What one runner call did, for budget and circuit-breaker accounting."""

    response: ArticleAnalysisResponse
    paid_attempt: bool
    reused: bool = False
    refused: bool = False
    job_state: str | None = None

    @property
    def stops_run(self) -> bool:
        return self.response.status == "unavailable"


class LedgeredArticleAnalysisRunner:
    """``ArticleAnalysisRunner`` that routes every analysis through the job ledger.

    ``allow_terminal_retry`` distinguishes an explicit per-article operator request (which may
    retry a failed, skipped, or baseline article) from automatic work (which never reopens a
    terminal job). Both reuse a stored current-contract analysis rather than paying again.
    """

    def __init__(
        self,
        repository: SQLiteRepository,
        analysis_service: ArticleEventAnalysisService,
        compatibility: ArticleAnalysisCompatibility,
        *,
        allow_terminal_retry: bool = False,
        owner_prefix: str = "runner",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self.analysis_service = analysis_service
        self.compatibility = compatibility
        self.allow_terminal_retry = allow_terminal_retry
        self.owner_prefix = owner_prefix
        self.clock = clock

    def analyze_article(self, article_id: str) -> ArticleAnalysisResponse:
        return self.process(article_id).response

    def process(self, article_id: str, now: datetime | None = None) -> LedgerOutcome:
        now = now or self.clock()
        contract = self.compatibility.contract_key
        article = self.repository.get_article(article_id)
        if article is None:
            return LedgerOutcome(
                ArticleAnalysisResponse(
                    article_id=article_id,
                    status="not_found",
                    message="The requested stored article was not found.",
                    failure_category="not_found",
                ),
                paid_attempt=False,
            )

        # A request for an article the ledger has never seen (for example a company that is not
        # under continuous coverage) still gets a job row, so its spend is recorded.
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

        existing = self.repository.latest_contract_analysis(article.fingerprint, self.compatibility)
        if existing is not None:
            self.repository.complete_analysis_job_from_existing(
                article.fingerprint, contract, analysis=existing, now=now
            )
            return LedgerOutcome(
                ArticleAnalysisResponse(article_id=article_id, status="cached", analysis=existing),
                paid_attempt=False,
                reused=True,
                job_state="analyzed",
            )

        owner = f"{self.owner_prefix}-{uuid4().hex}"
        claimed = self.repository.claim_analysis_job(
            article.fingerprint,
            contract,
            owner=owner,
            now=now,
            lease_expires_at=now + LEASE_DURATION,
            allow_terminal_retry=self.allow_terminal_retry,
        )
        if claimed is None:
            return self._refusal(article_id, contract)

        usage = _usage_store(self.analysis_service)
        if usage is not None:
            usage.clear()
        response = self.analysis_service.analyze_article(article_id)
        input_tokens, output_tokens = _usage_totals(usage)
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
        response: ArticleAnalysisResponse,
        input_tokens: int,
        output_tokens: int,
        now: datetime,
    ) -> LedgerOutcome:
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
                analysis=response.analysis,
                **common,
            )
            return LedgerOutcome(
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
            return LedgerOutcome(response, paid_attempt=False, job_state="pending")

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
        return LedgerOutcome(response, paid_attempt=paid, job_state=state)

    def _refusal(self, article_id: str, contract: str) -> LedgerOutcome:
        job = self.repository.get_analysis_job(article_id, contract)
        state = job.state if job is not None else "unknown"
        if state == "leased":
            message = "This article is already being analysed by another process."
        elif state == "retry_wait" and job is not None and job.next_attempt_at is not None:
            message = f"A retry is already scheduled for {job.next_attempt_at.isoformat()}."
        elif state in JOB_TERMINAL_STATES:
            message = f"The analysis job for this article is terminal ({state})."
        else:
            message = f"The analysis job for this article is not claimable ({state})."
        return LedgerOutcome(
            ArticleAnalysisResponse(
                article_id=article_id,
                status="failed",
                message=message,
                failure_category=f"ledger_{state}",
            ),
            paid_attempt=False,
            refused=True,
            job_state=state,
        )


def _usage_store(service: ArticleEventAnalysisService) -> dict | None:
    usage = getattr(getattr(service, "provider", None), "last_usage", None)
    return usage if isinstance(usage, dict) else None


def _usage_totals(usage: dict | None) -> tuple[int, int]:
    """Sum per-stage (input, output) token counts the provider recorded for the last call."""

    if not usage:
        return 0, 0
    input_tokens = output_tokens = 0
    for value in usage.values():
        if isinstance(value, tuple) and len(value) == 2:
            input_tokens += int(value[0] or 0)
            output_tokens += int(value[1] or 0)
    return input_tokens, output_tokens
