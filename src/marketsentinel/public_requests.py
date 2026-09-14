"""Shared public requests: "start coverage" for a company and "analyse" for a stored article.

The public deployment is a read-only window onto a snapshot and holds no LLM credential. What it
*can* do is record that someone asked for coverage of a company, or for the analysis of one
stored article, so that the private scheduled worker -- the only writer, the only place an
OpenAI key exists -- picks the request up on its next run. Requests are global and anonymous:
nothing about the requester is recorded, and once a request is processed everyone sees the same
result through the ordinary published snapshot.

Two halves live here because they share one key layout and one validation vocabulary:

- **The public side** (``PublicRequestService``): validates a request against the constituent
  universe and the stored corpus, applies the caps that keep strangers from creating unbounded
  work, and drops one small JSON object per request into a ``RequestSink``. The sink is the only
  durable state: the public host's disk is ephemeral (it is reset on every restart, and every
  publish restarts it), so the service keeps only an in-memory mirror of what is pending and
  re-reads the sink at startup.
- **The worker side** (``admit_public_requests``): reads the synced request objects, activates
  requested companies through the existing coverage ledger and analyses requested articles
  through the existing job ledger -- under hard per-run caps -- and reports exactly which keys it
  consumed so the workflow deletes those and leaves the rest queued for a later run.

Spend is bounded on the worker, never merely discouraged on the public host: a request can only
ever *ask*; the worker's caps decide what is paid for in one run.

Key layout, shared by both halves and by the workflow's ``aws s3`` steps::

    coverage/<TICKER>.json
    articles/<TICKER>/<article_id>.json

Idempotent by construction: a repeated request for the same company or article overwrites the
same key, so the number of distinct requests is bounded by the universe plus the stored corpus.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from marketsentinel.analysis_compatibility import ArticleAnalysisCompatibility
from marketsentinel.analysis_ledger import LedgeredArticleAnalysisRunner
from marketsentinel.domain import ArticleAnalysisRequestView, CoverageRequestView
from marketsentinel.errors import ConstituentNotFoundError, MarketSentinelError
from marketsentinel.storage.sqlite import SQLiteRepository
from marketsentinel.timeutils import ensure_utc, utc_now

LOGGER = logging.getLogger(__name__)

COVERAGE_PREFIX = "coverage/"
ARTICLES_PREFIX = "articles/"

# What a key may contain. Tickers are index symbols (``BRK.B``, ``BT.A``); article ids are the
# hex fingerprints ``normalization.article_fingerprint`` produces. Anything else is not a request
# this system made and is discarded unread on the worker.
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")
_ARTICLE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")

# A ceiling on how many keys one listing reads. The caps below keep the real count far smaller;
# this only bounds the damage if the sink were filled by something other than this service.
_MAX_LISTED_KEYS = 2000

# The public host talks to the request store on the request path (one put per request) and once
# at startup (the listing that rebuilds the pending mirror, inside the app's lifespan). boto3's
# defaults -- 60 s connect, 60 s read, several attempts -- would let an unreachable store hold a
# request thread or the whole startup for minutes. These bound one operation to roughly half a
# minute in the worst case; a failure past that is the same 503 either way.
R2_CONNECT_TIMEOUT_SECONDS = 5
R2_READ_TIMEOUT_SECONDS = 10
R2_MAX_ATTEMPTS = 2

# The worker's explicit-request pass stops after this many consecutive paid failures, exactly like
# the coverage cycle's own breaker (coverage_cycle.py::_analyze): a provider that is failing every
# call -- an expired key, an exhausted quota, an outage that is not reported as "unavailable" --
# must not burn the whole per-run cap on attempts that cannot succeed. Requests it did not reach
# stay queued for a later run.
CIRCUIT_BREAKER_CONSECUTIVE_FAILURES = 2

RATE_WINDOW = timedelta(minutes=1)

# Redaction for request-store failure logs. A SigV4 Authorization header can reach an exception's
# text: a credential containing a newline makes the HTTP client reject the header and quote it
# (verified offline against botocore), so the key id and signature would otherwise be logged.
_CREDENTIAL_PATTERN = re.compile(r"(Credential=)[^,\s'\"]*")
_SIGNATURE_PATTERN = re.compile(r"(Signature=)[0-9A-Za-z]+")
_REDACTED = "[redacted]"


# --------------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedRequestKey:
    kind: str  # "coverage" | "article"
    ticker: str
    article_id: str | None = None


def coverage_request_key(ticker: str) -> str:
    return f"{COVERAGE_PREFIX}{ticker}.json"


def article_request_key(ticker: str, article_id: str) -> str:
    return f"{ARTICLES_PREFIX}{ticker}/{article_id}.json"


def parse_request_key(key: str) -> ParsedRequestKey | None:
    """Recover (kind, ticker, article id) from a key, or ``None`` for anything malformed."""

    if not key.endswith(".json"):
        return None
    body = key[: -len(".json")]
    if body.startswith(COVERAGE_PREFIX):
        ticker = body[len(COVERAGE_PREFIX) :]
        return ParsedRequestKey("coverage", ticker) if _TICKER.match(ticker) else None
    if body.startswith(ARTICLES_PREFIX):
        parts = body[len(ARTICLES_PREFIX) :].split("/")
        if len(parts) != 2:
            return None
        ticker, article_id = parts
        if _TICKER.match(ticker) and _ARTICLE_ID.match(article_id):
            return ParsedRequestKey("article", ticker, article_id)
    return None


# --------------------------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------------------------


class RequestSinkError(MarketSentinelError):
    """The durable request store could not be written or listed."""


class RequestSink(Protocol):
    def put(self, key: str, payload: dict[str, Any]) -> None: ...

    def list_keys(self) -> list[str]: ...


class DirectoryRequestSink:
    """Requests as files under one directory, in the same layout as the R2 keys.

    Used by the worker (the workflow ``aws s3 sync``s the bucket into a directory first, so the
    admission step needs no cloud credential and is testable offline) and by local runs.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put(self, key: str, payload: dict[str, Any]) -> None:
        target = self.root / key
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_suffix(".json.tmp")
            staging.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            staging.replace(target)
        except OSError as exc:
            raise RequestSinkError(f"could not record the request: {exc}") from exc

    def list_keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        keys = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*.json")
            if path.is_file()
        )
        return keys[:_MAX_LISTED_KEYS]

    def read(self, key: str) -> dict[str, Any] | None:
        """The stored payload, or ``None`` when it is missing or not a JSON object."""

        try:
            loaded = json.loads((self.root / key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None


class R2RequestSink:
    """Requests as objects in one dedicated S3-compatible (Cloudflare R2) bucket.

    The credential this uses should be scoped to that bucket alone: it can then only ever write
    request objects, never the private corpus or the published snapshot. boto3 is imported lazily
    so a deployment without a bucket never loads it.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        client: Any | None = None,
    ) -> None:
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = client

    def _s3(self) -> Any:
        if self._client is None:
            import boto3  # noqa: PLC0415 -- only the public deployment with a bucket needs it
            from botocore.config import Config  # noqa: PLC0415

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key_id,
                aws_secret_access_key=self._secret_access_key,
                region_name="auto",
                config=Config(
                    connect_timeout=R2_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=R2_READ_TIMEOUT_SECONDS,
                    retries={"max_attempts": R2_MAX_ATTEMPTS, "mode": "standard"},
                ),
            )
        return self._client

    def put(self, key: str, payload: dict[str, Any]) -> None:
        try:
            self._s3().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(payload, sort_keys=True).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:  # boto3 raises many concrete types; none is recoverable here
            self._log_failure("put_object", exc, key=key)
            raise RequestSinkError("could not record the request in the request store") from exc

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        try:
            paginator = self._s3().get_paginator("list_objects_v2")
            for prefix in (COVERAGE_PREFIX, ARTICLES_PREFIX):
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                    for item in page.get("Contents", []):
                        keys.append(str(item["Key"]))
                        if len(keys) >= _MAX_LISTED_KEYS:
                            return keys
        except Exception as exc:
            self._log_failure("list_objects_v2", exc)
            raise RequestSinkError("could not list the request store") from exc
        return keys

    def _log_failure(self, operation: str, exc: BaseException, *, key: str | None = None) -> None:
        """Log the real cause of a store failure without any credential material.

        Logged: the exception type, the provider's error code, message, HTTP status and request
        id, the bucket, key and endpoint (none is a credential), whether any configured value has
        surrounding whitespace (settings are not stripped, and whitespace breaks signing or
        endpoint parsing), and the traceback with the access key id, the secret, and any SigV4
        ``Credential=`` / ``Signature=`` value redacted. Never logged: the provider's full error
        body, which for a signature failure also carries ``AWSAccessKeyId``, ``StringToSign``
        and ``CanonicalRequest``. Logged at ERROR with an explicitly redacted traceback rather
        than ``LOGGER.exception``, whose raw traceback could quote an Authorization header.
        """

        LOGGER.error(
            "public requests: R2 %s failed: %s bucket=%r key=%r endpoint=%r "
            "whitespace(endpoint=%s, access_key_id=%s, secret=%s, bucket=%s)\n%s",
            operation,
            self._redact(_safe_error_details(exc)),
            self.bucket,
            key,
            self._endpoint_url,
            _has_surrounding_whitespace(self._endpoint_url),
            _has_surrounding_whitespace(self._access_key_id),
            _has_surrounding_whitespace(self._secret_access_key),
            _has_surrounding_whitespace(self.bucket),
            self._redact("".join(traceback.format_exception(exc)).rstrip()),
        )

    def _redact(self, text: str) -> str:
        for secret in (self._secret_access_key, self._access_key_id):
            for variant in {secret, secret.strip()}:
                if variant:
                    text = text.replace(variant, _REDACTED)
        text = _CREDENTIAL_PATTERN.sub(rf"\g<1>{_REDACTED}", text)
        return _SIGNATURE_PATTERN.sub(rf"\g<1>{_REDACTED}", text)


def _has_surrounding_whitespace(value: str | None) -> bool:
    return bool(value) and value != value.strip()


def _safe_error_details(exc: BaseException) -> str:
    """Exception type plus an allowlist of provider error fields (botocore ``ClientError``)."""

    parts = [f"type={type(exc).__module__}.{type(exc).__qualname__}"]
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
        metadata = response.get("ResponseMetadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        for label, value in (
            ("code", error.get("Code")),
            ("message", error.get("Message")),
            ("http_status", metadata.get("HTTPStatusCode")),
            ("request_id", metadata.get("RequestId")),
        ):
            if value not in (None, ""):
                parts.append(f"{label}={value!r}")
    else:
        parts.append(f"detail={str(exc)[:500]!r}")
    return " ".join(parts)


# --------------------------------------------------------------------------------------------
# Public side
# --------------------------------------------------------------------------------------------


class PublicRequestError(MarketSentinelError):
    status_code = 400


class RequestNotFoundError(PublicRequestError):
    status_code = 404


class RequestRejectedError(PublicRequestError):
    status_code = 409


class RequestLimitError(PublicRequestError):
    status_code = 429


@dataclass(frozen=True)
class RequestLimits:
    """The public-side caps. Every one is a bound on *asking*; spend is capped on the worker."""

    rate_limit_per_minute: int
    max_pending_coverage: int
    max_pending_articles: int
    max_covered_companies: int


class SlidingWindowRateLimiter:
    """One process-wide sliding window: at most ``limit`` accepted calls per ``window``."""

    def __init__(self, limit: int, window: timedelta = RATE_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._accepted: deque[datetime] = deque()

    def allow(self, now: datetime) -> bool:
        cutoff = now - self.window
        while self._accepted and self._accepted[0] <= cutoff:
            self._accepted.popleft()
        if len(self._accepted) >= self.limit:
            return False
        self._accepted.append(now)
        return True


@dataclass(frozen=True)
class PendingRequests:
    covered_companies: tuple[str, ...]
    coverage: tuple[str, ...]
    articles: tuple[str, ...]


COVERAGE_QUEUED_MESSAGE = (
    "Coverage has been requested. The scheduled worker starts shared coverage on its next run "
    "and everyone sees the result once it is published."
)
COVERAGE_ALREADY_QUEUED_MESSAGE = "Coverage is already queued for the next scheduled run."
COVERAGE_ACTIVE_MESSAGE = "This company is already under shared coverage."
ARTICLE_QUEUED_MESSAGE = (
    "Analysis has been requested. The scheduled worker analyses this article on its next run "
    "and everyone sees the result once it is published."
)
ARTICLE_ALREADY_QUEUED_MESSAGE = "This article is already queued for analysis."
ARTICLE_ANALYSED_MESSAGE = "This article already has a stored analysis."


class PublicRequestService:
    """Validate, cap, and record shared requests. Never spends and never generates anything."""

    def __init__(
        self,
        *,
        sink: RequestSink,
        repository: SQLiteRepository,
        constituents: Any,
        compatibility: ArticleAnalysisCompatibility,
        limits: RequestLimits,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.sink = sink
        self.repository = repository
        self.constituents = constituents
        self.compatibility = compatibility
        self.limits = limits
        self.clock = clock
        self._pending_coverage: set[str] = set()
        self._pending_articles: dict[str, str] = {}
        self._rate = SlidingWindowRateLimiter(limits.rate_limit_per_minute)
        self._lock = threading.Lock()

    def rehydrate(self) -> int:
        """Rebuild the in-memory mirror from the sink. Never raises: a listing failure leaves the
        mirror empty, which only means a repeat request re-writes an identical key."""

        try:
            keys = self.sink.list_keys()
        except RequestSinkError as exc:
            LOGGER.warning("public requests: could not list the request store (%s)", exc)
            return 0
        with self._lock:
            self._pending_coverage.clear()
            self._pending_articles.clear()
            for key in keys:
                parsed = parse_request_key(key)
                if parsed is None:
                    continue
                if parsed.kind == "coverage":
                    self._pending_coverage.add(parsed.ticker)
                elif parsed.article_id is not None:
                    self._pending_articles[parsed.article_id] = parsed.ticker
            return len(self._pending_coverage) + len(self._pending_articles)

    def pending(self) -> PendingRequests:
        covered = tuple(
            sorted(item.ticker for item in self.repository.list_company_coverage() if item.active)
        )
        with self._lock:
            return PendingRequests(
                covered_companies=covered,
                coverage=tuple(sorted(self._pending_coverage)),
                articles=tuple(sorted(self._pending_articles)),
            )

    def request_coverage(self, symbol: str) -> CoverageRequestView:
        constituent = self.constituents.resolve_cached(symbol)
        ticker = _key_safe_ticker(constituent.symbol)
        coverage = self.repository.get_company_coverage(ticker)
        if coverage is not None and coverage.active:
            return CoverageRequestView(
                symbol=ticker, state="covered", message=COVERAGE_ACTIVE_MESSAGE
            )
        active_count = sum(1 for item in self.repository.list_company_coverage() if item.active)
        now = self.clock()
        with self._lock:
            if ticker in self._pending_coverage:
                return CoverageRequestView(
                    symbol=ticker, state="already_queued", message=COVERAGE_ALREADY_QUEUED_MESSAGE
                )
            if len(self._pending_coverage) >= self.limits.max_pending_coverage:
                raise RequestLimitError(
                    "The coverage request queue is full. Try again after the next scheduled run."
                )
            if active_count + len(self._pending_coverage) >= self.limits.max_covered_companies:
                raise RequestLimitError(
                    "Shared coverage capacity has been reached for now. Try again later."
                )
            self._check_rate(now)
            self.sink.put(
                coverage_request_key(ticker),
                {"kind": "coverage", "ticker": ticker, "requested_at": now.isoformat()},
            )
            self._pending_coverage.add(ticker)
        return CoverageRequestView(symbol=ticker, state="queued", message=COVERAGE_QUEUED_MESSAGE)

    def request_article_analysis(self, symbol: str, article_id: str) -> ArticleAnalysisRequestView:
        constituent = self.constituents.resolve_cached(symbol)
        ticker = _key_safe_ticker(constituent.symbol)
        article = self.repository.get_article(article_id)
        if article is None or article.ticker != ticker or not _ARTICLE_ID.match(article_id):
            raise RequestNotFoundError("No stored article with that id exists for this company.")
        if article.is_demo:
            raise RequestRejectedError("Demo articles are never analysed.")
        if self.repository.latest_contract_analysis(article.fingerprint, self.compatibility):
            return ArticleAnalysisRequestView(
                article_id=article_id, state="analysed", message=ARTICLE_ANALYSED_MESSAGE
            )
        now = self.clock()
        with self._lock:
            if article_id in self._pending_articles:
                return ArticleAnalysisRequestView(
                    article_id=article_id,
                    state="already_queued",
                    message=ARTICLE_ALREADY_QUEUED_MESSAGE,
                )
            if len(self._pending_articles) >= self.limits.max_pending_articles:
                raise RequestLimitError(
                    "The analysis request queue is full. Try again after the next scheduled run."
                )
            self._check_rate(now)
            self.sink.put(
                article_request_key(ticker, article_id),
                {
                    "kind": "article",
                    "ticker": ticker,
                    "article_id": article_id,
                    "requested_at": now.isoformat(),
                },
            )
            self._pending_articles[article_id] = ticker
        return ArticleAnalysisRequestView(
            article_id=article_id, state="queued", message=ARTICLE_QUEUED_MESSAGE
        )

    def _check_rate(self, now: datetime) -> None:
        if not self._rate.allow(now):
            raise RequestLimitError("Too many requests right now. Try again in a minute.")


def _key_safe_ticker(ticker: str) -> str:
    """The resolved symbol, provided it fits the key grammar the worker accepts.

    Every index symbol does. The guard exists so that a symbol that somehow did not (a slash, a
    space) can neither escape the key layout in a directory sink nor be written only to be
    discarded unread on the worker; it is refused here instead, with nothing recorded.
    """

    if not _TICKER.match(ticker):
        raise RequestRejectedError("This company's symbol cannot be requested.")
    return ticker


# --------------------------------------------------------------------------------------------
# Worker side
# --------------------------------------------------------------------------------------------


class CoverageActivator(Protocol):
    def activate(self, symbol: str, *, now: datetime) -> Any: ...


@dataclass(frozen=True)
class AdmissionReport:
    """What one admission pass did. ``consumed_keys`` is the exact set the workflow may delete."""

    activated: tuple[str, ...] = ()
    already_covered: tuple[str, ...] = ()
    deferred_coverage: tuple[str, ...] = ()
    articles: tuple[tuple[str, str], ...] = ()  # (article_id, resulting status)
    deferred_articles: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()  # (key, reason)
    consumed_keys: tuple[str, ...] = ()
    stop_reason: str | None = None

    def render(self) -> str:
        lines = [
            "Public request admission",
            f"  activated: {', '.join(self.activated) or 'none'}",
            f"  already covered: {', '.join(self.already_covered) or 'none'}",
            f"  deferred coverage (cap): {', '.join(self.deferred_coverage) or 'none'}",
        ]
        for article_id, status in self.articles:
            lines.append(f"  article {article_id}: {status}")
        if self.deferred_articles:
            lines.append(f"  deferred article requests (cap): {len(self.deferred_articles)}")
        for key, reason in self.rejected:
            lines.append(f"  rejected {key}: {reason}")
        if self.stop_reason:
            lines.append(f"  stopped: {self.stop_reason}")
        lines.append(f"  consumed keys: {len(self.consumed_keys)}")
        return "\n".join(lines)


@dataclass
class _Loaded:
    key: str
    parsed: ParsedRequestKey
    requested_at: datetime


def _load_requests(sink: DirectoryRequestSink) -> tuple[list[_Loaded], list[tuple[str, str]]]:
    loaded: list[_Loaded] = []
    rejected: list[tuple[str, str]] = []
    for key in sink.list_keys():
        parsed = parse_request_key(key)
        if parsed is None:
            rejected.append((key, "malformed key"))
            continue
        payload = sink.read(key)
        if payload is None:
            rejected.append((key, "unreadable payload"))
            continue
        try:
            requested_at = ensure_utc(datetime.fromisoformat(str(payload.get("requested_at"))))
        except (TypeError, ValueError):
            # An unreadable timestamp sorts last rather than jumping the queue.
            requested_at = ensure_utc(datetime(9999, 1, 1))
        loaded.append(_Loaded(key=key, parsed=parsed, requested_at=requested_at))
    loaded.sort(key=lambda item: (item.requested_at, item.key))
    return loaded, rejected


def admit_public_requests(
    *,
    sink: DirectoryRequestSink,
    coverage: CoverageActivator,
    runner: LedgeredArticleAnalysisRunner,
    repository: SQLiteRepository,
    now: datetime,
    max_new_tickers: int,
    max_article_requests: int,
) -> AdmissionReport:
    """Admit synced requests under hard caps. Only consumed keys may be deleted afterwards.

    Coverage requests, oldest first: an already-active ticker is consumed as a no-op; up to
    ``max_new_tickers`` others are activated (idempotent, spends nothing -- the cycle that follows
    pays under its own caps); the rest stay queued. Article requests, oldest first: up to
    ``max_article_requests`` run through the explicit-request ledger runner, which reuses a
    stored analysis rather than paying again; a failure is recorded in the ledger and the key is
    still consumed, so a permanently failing article costs at most one bounded attempt per
    request. An unconfigured provider stops the pass and leaves the remaining keys queued, and so
    does the circuit breaker after ``CIRCUIT_BREAKER_CONSECUTIVE_FAILURES`` consecutive paid
    failures.
    """

    if max_new_tickers < 0 or max_article_requests < 0:
        raise ValueError("caps must not be negative")
    loaded, rejected = _load_requests(sink)
    consumed: list[str] = [key for key, _ in rejected]
    activated: list[str] = []
    already: list[str] = []
    deferred_coverage: list[str] = []
    articles: list[tuple[str, str]] = []
    deferred_articles: list[str] = []
    stop_reason: str | None = None
    consecutive_failures = 0

    for item in (entry for entry in loaded if entry.parsed.kind == "coverage"):
        ticker = item.parsed.ticker
        existing = repository.get_company_coverage(ticker)
        if existing is not None and existing.active:
            already.append(ticker)
            consumed.append(item.key)
            continue
        if len(activated) >= max_new_tickers:
            deferred_coverage.append(ticker)
            continue
        try:
            coverage.activate(ticker, now=now)
        except ConstituentNotFoundError as exc:
            rejected.append((item.key, str(exc)))
            consumed.append(item.key)
            continue
        activated.append(ticker)
        consumed.append(item.key)

    for item in (entry for entry in loaded if entry.parsed.kind == "article"):
        article_id = item.parsed.article_id or ""
        if stop_reason is not None or len(articles) >= max_article_requests:
            deferred_articles.append(article_id)
            continue
        article = repository.get_article(article_id)
        if article is None or article.ticker != item.parsed.ticker:
            rejected.append((item.key, "no such stored article for that ticker"))
            consumed.append(item.key)
            continue
        outcome = runner.process(article_id, now=now)
        if outcome.stops_run:
            # Nothing was spent and the key is not consumed: the next run retries it.
            stop_reason = "provider_unavailable"
            deferred_articles.append(article_id)
            continue
        articles.append((article_id, outcome.job_state or outcome.response.status))
        consumed.append(item.key)
        if outcome.reused or outcome.response.status in ("generated", "cached"):
            consecutive_failures = 0
        elif outcome.paid_attempt:
            consecutive_failures += 1
            if consecutive_failures >= CIRCUIT_BREAKER_CONSECUTIVE_FAILURES:
                stop_reason = "circuit_breaker"

    return AdmissionReport(
        activated=tuple(activated),
        already_covered=tuple(already),
        deferred_coverage=tuple(deferred_coverage),
        articles=tuple(articles),
        deferred_articles=tuple(deferred_articles),
        rejected=tuple(rejected),
        consumed_keys=tuple(dict.fromkeys(consumed)),
        stop_reason=stop_reason,
    )


@dataclass(frozen=True)
class TickerCycleOrder:
    """Pure ordering helper input: a ticker and the oldest attempt any of its providers made."""

    ticker: str
    oldest_attempt_at: datetime | None = None


def order_tickers_for_cycle(entries: list[TickerCycleOrder]) -> list[str]:
    """Never-cycled tickers first, then least recently attempted; ties break alphabetically.

    With a per-run ticker cap this is round-robin: whichever tickers a capped run leaves out are
    exactly the ones at the front of the next run.
    """

    def sort_key(entry: TickerCycleOrder) -> tuple[int, datetime, str]:
        if entry.oldest_attempt_at is None:
            return (0, datetime.min, entry.ticker)
        return (1, ensure_utc(entry.oldest_attempt_at).replace(tzinfo=None), entry.ticker)

    return [entry.ticker for entry in sorted(entries, key=sort_key)]
