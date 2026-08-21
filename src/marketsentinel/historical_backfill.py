"""Pure planning and reporting shapes for historical intelligence backfill.

No I/O happens here -- bucket boundaries and deterministic report dataclasses only.
``backfill_service.HistoricalIntelligenceBackfillService`` performs the actual fetch/persist/
analyze work using these plans. Kept separate so bucket boundaries and report rendering stay
independently unit-testable without a database or network access.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

BackfillFetchStatus = Literal["ok", "partial", "failed"]


@dataclass(frozen=True)
class BackfillBucket:
    """One calendar-month slice of a historical backfill horizon, clipped to the requested span."""

    label: str
    start: datetime
    end: datetime


def plan_backfill_buckets(now: datetime, horizon_days: int = 366) -> list[BackfillBucket]:
    """Split ``[now - horizon_days, now]`` into calendar-month buckets, oldest first.

    Buckets are clipped to the horizon at both ends so no fetch call is ever asked for a date
    outside what was actually requested. A bucket is only produced when it has positive width,
    so a ``now`` that lands exactly on a month boundary never yields a zero-width trailing bucket.
    """

    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    horizon_start = now - timedelta(days=horizon_days)
    buckets: list[BackfillBucket] = []
    cursor = horizon_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < now:
        month_start = cursor
        month_end = _add_one_month(month_start)
        bucket_start = max(month_start, horizon_start)
        bucket_end = min(month_end, now)
        if bucket_end > bucket_start:
            buckets.append(
                BackfillBucket(
                    label=f"{month_start.year:04d}-{month_start.month:02d}",
                    start=bucket_start,
                    end=bucket_end,
                )
            )
        cursor = month_end
    return buckets


def _add_one_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


@dataclass(frozen=True)
class BackfillBucketReport:
    """What actually happened for one bucket -- never implies success for an unattempted month."""

    bucket: BackfillBucket
    fetch_status: BackfillFetchStatus
    articles_fetched: int = 0
    distinct_publishers: int = 0
    candidates_selected: int = 0
    analyses_completed: int = 0
    analyses_failed: int = 0
    sentiment_dates_produced: int = 0
    message: str | None = None


@dataclass(frozen=True)
class BackfillRunReport:
    """A deterministic, human-readable run summary -- printed/logged, not yet a database table.

    A 1Y request must never be presented as fully covered when a month failed or was skipped
    after the run-level budget/circuit-breaker stopped new analysis; every bucket's own
    ``fetch_status``/``message`` makes partial coverage explicit.
    """

    ticker: str
    horizon_days: int
    buckets: tuple[BackfillBucketReport, ...]
    new_analyses_attempted: int
    circuit_breaker_tripped: bool
    sentiment_dates_total: int

    def render(self) -> str:
        lines = [
            f"Historical backfill report for {self.ticker} (horizon: {self.horizon_days} days)"
        ]
        for report in self.buckets:
            line = (
                f"  {report.bucket.label}  {report.fetch_status:8s} "
                f"articles={report.articles_fetched} publishers={report.distinct_publishers} "
                f"candidates={report.candidates_selected} analyzed={report.analyses_completed} "
                f"failed={report.analyses_failed} sentiment_dates={report.sentiment_dates_produced}"
            )
            if report.message:
                line += f"  ({report.message})"
            lines.append(line)
        lines.append(
            f"Total new analyses attempted: {self.new_analyses_attempted} | "
            f"circuit breaker tripped: {self.circuit_breaker_tripped} | "
            f"sentiment dates produced across the run: {self.sentiment_dates_total}"
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class EvidenceRefreshReport:
    """Result of passing current-compatible analyses back through analyze_article after an
    evidence-selection change. Conceptually separate from StaleBacklogReport: this operates
    over analyses that already display-compatible, using analyze_article's own
    accepts_for_cache/evidence_fingerprint check as the sole arbiter of whether a given
    article's evidence actually changed -- an unchanged fingerprint costs nothing.
    """

    ticker: str
    candidates_checked: int
    cache_hits: int
    regenerated: int
    failed: int
    circuit_breaker_tripped: bool

    def render(self) -> str:
        return (
            f"Evidence-refresh report for {self.ticker}\n"
            f"  current-compatible analyses checked: {self.candidates_checked}\n"
            f"  unchanged (free cache hit): {self.cache_hits}  "
            f"regenerated (evidence changed): {self.regenerated}  failed: {self.failed}\n"
            f"  circuit breaker tripped: {self.circuit_breaker_tripped}"
        )


@dataclass(frozen=True)
class StaleBacklogReport:
    """Result of a bounded, manually-triggered reanalysis pass over version-orphaned articles."""

    ticker: str
    backlog_size: int
    attempted: int
    reanalyzed: int
    failed: int
    circuit_breaker_tripped: bool

    def render(self) -> str:
        return (
            f"Stale-version catch-up report for {self.ticker}\n"
            f"  backlog size (stored, incompatible with the running version): {self.backlog_size}\n"
            f"  attempted: {self.attempted}  now current-version compatible: {self.reanalyzed}  "
            f"failed: {self.failed}\n"
            f"  circuit breaker tripped: {self.circuit_breaker_tripped}"
        )
