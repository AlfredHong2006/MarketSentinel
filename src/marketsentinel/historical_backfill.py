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


class BackfillRefusal(ValueError):
    """A run that must not start: raised before any fetch, with a message for the operator."""


def plan_backfill_buckets(
    now: datetime,
    horizon_days: int = 366,
    offset_days: int = 0,
    until: datetime | None = None,
) -> list[BackfillBucket]:
    """Split ``[now - horizon_days, now - offset_days]`` into calendar-month buckets, oldest first.

    Buckets are clipped to the horizon at both ends so no fetch call is ever asked for a date
    outside what was actually requested. A bucket is only produced when it has positive width,
    so a ``now`` that lands exactly on a month boundary never yields a zero-width trailing bucket.

    ``offset_days`` skips the most recent days of the horizon. Only the end of the span moves: the
    horizon start and every calendar-month boundary are exactly the ones a plain
    ``offset_days=0`` run of the same ``now`` and ``horizon_days`` would plan, so a deeper range
    fetched later lines up with it. The bucket straddling ``now - offset_days`` is clipped there,
    and the clipped remainder belongs to the range ``[now - offset_days, now]`` that was skipped.

    ``until`` ends the span at an exact instant instead (the start of already-stored history). It
    follows the same rule -- only the end moves -- and is exclusive of ``offset_days``. It must lie
    inside ``(now - horizon_days, now]``.
    """

    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if offset_days < 0:
        raise ValueError("offset_days must not be negative")
    if offset_days >= horizon_days:
        raise ValueError(
            f"offset_days ({offset_days}) must be smaller than horizon_days ({horizon_days}); "
            "an offset that reaches the horizon leaves nothing to plan"
        )
    horizon_start = now - timedelta(days=horizon_days)
    range_end = now - timedelta(days=offset_days)
    if until is not None:
        if offset_days:
            raise ValueError("offset_days and until are mutually exclusive")
        if not horizon_start < until <= now:
            raise ValueError(
                f"until ({until.isoformat()}) must lie after the horizon start "
                f"({horizon_start.isoformat()}) and not after now ({now.isoformat()})"
            )
        range_end = until
    buckets: list[BackfillBucket] = []
    cursor = horizon_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor < range_end:
        month_start = cursor
        month_end = _add_one_month(month_start)
        bucket_start = max(month_start, horizon_start)
        bucket_end = min(month_end, range_end)
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


def resolve_backfill_boundary(
    *,
    ticker: str,
    stored_start: datetime | None,
    override: datetime | None,
    now: datetime,
    horizon_days: int,
) -> datetime:
    """The exclusive end of a boundary-anchored run: the override, else the stored start.

    Pure. Refuses (``BackfillRefusal``) when the ticker has no stored article to anchor on -- even
    with an override, since an empty corpus means this is a first backfill, not a deeper one -- or
    when the boundary does not lie inside the horizon, where there is nothing to fetch.
    """

    if stored_start is None:
        raise BackfillRefusal(
            f"{ticker} has no stored non-demo articles, so there is no stored history to extend. "
            "Run a plain backfill (without --until-stored-start / --until) instead."
        )
    boundary = override if override is not None else stored_start
    horizon_start = now - timedelta(days=horizon_days)
    if not horizon_start < boundary <= now:
        origin = "--until" if override is not None else "the earliest stored article"
        raise BackfillRefusal(
            f"The boundary ({origin}: {boundary.isoformat()}) is outside the horizon "
            f"({horizon_start.isoformat()} to {now.isoformat()}), so there is nothing to fetch. "
            "Check --months, the boundary, or whether this range is already stored."
        )
    return boundary


@dataclass(frozen=True)
class BackfillPlanReport:
    """What a boundary-anchored run would do, computed from the database alone (no fetch)."""

    ticker: str
    now: datetime
    horizon_days: int
    boundary: datetime
    boundary_source: str
    stored_start: datetime
    earliest_published: tuple[datetime, ...]
    stored_total: int
    stored_in_30_days_after_boundary: int
    scored_in_horizon: int
    read_cap: int
    buckets: tuple[BackfillBucket, ...]

    def render(self) -> str:
        age_days = (self.now - self.boundary).days
        lines = [
            f"Backfill plan for {self.ticker} (plan only: no network call, nothing written)",
            f"  run time (now): {self.now.isoformat()}  horizon: {self.horizon_days} days",
            f"  boundary: {self.boundary.isoformat()} ({self.boundary_source}; {age_days} days "
            "before now). Nothing at or after it will be stored.",
            f"  earliest stored non-demo article: {self.stored_start.isoformat()}",
            "  five earliest stored published_at values:",
        ]
        lines.extend(f"    {value.isoformat()}" for value in self.earliest_published)
        lines.append(
            f"  stored non-demo articles in the 30 days after the boundary: "
            f"{self.stored_in_30_days_after_boundary} (of {self.stored_total} stored in total)"
        )
        scored = (
            f"more than {self.read_cap}"
            if self.scored_in_horizon > self.read_cap
            else str(self.scored_in_horizon)
        )
        lines.append(
            f"  scored articles stored since the horizon start: {scored} "
            f"(a run refuses to start at {self.read_cap} or more)"
        )
        lines.append(f"  planned buckets: {len(self.buckets)}")
        lines.extend(
            f"    {bucket.label}  {bucket.start.isoformat()} -> {bucket.end.isoformat()}"
            for bucket in self.buckets
        )
        return "\n".join(lines)


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
    offset_days: int = 0
    until: datetime | None = None

    def render(self) -> str:
        scope = f"horizon: {self.horizon_days} days"
        if self.offset_days:
            scope += f", skipping the most recent {self.offset_days} days"
        if self.until is not None:
            scope += f", ending exactly at {self.until.isoformat()} (nothing at or after it)"
        lines = [f"Historical backfill report for {self.ticker} ({scope})"]
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
class SelectionGapBucketReport:
    """What one bucket contributed to a no-fetch selection-gap fill."""

    bucket: BackfillBucket
    stored_articles: int = 0
    candidates_selected: int = 0
    cache_hits: int = 0
    newly_analyzed: int = 0
    failed: int = 0
    message: str | None = None


@dataclass(frozen=True)
class SelectionGapReport:
    """Result of re-running candidate selection over already-stored articles only.

    ``as_of`` is recorded because bucket geometry follows from it: the same stored corpus
    replanned against a different ``as_of`` yields different buckets, so a report that omitted it
    could not be compared with the run it was meant to repair.
    """

    ticker: str
    horizon_days: int
    as_of: datetime
    buckets: tuple[SelectionGapBucketReport, ...]
    new_analyses_attempted: int
    circuit_breaker_tripped: bool

    def render(self) -> str:
        lines = [
            f"Selection-gap fill report for {self.ticker} "
            f"(as of {self.as_of.isoformat()}, horizon: {self.horizon_days} days, no fetch)"
        ]
        for report in self.buckets:
            line = (
                f"  {report.bucket.label}  stored={report.stored_articles} "
                f"candidates={report.candidates_selected} cached={report.cache_hits} "
                f"new={report.newly_analyzed} failed={report.failed}"
            )
            if report.message:
                line += f"  ({report.message})"
            lines.append(line)
        lines.append(
            f"Totals: cache hits {sum(item.cache_hits for item in self.buckets)} | "
            f"newly analyzed {sum(item.newly_analyzed for item in self.buckets)} | "
            f"failed {sum(item.failed for item in self.buckets)} | "
            f"new analyses attempted: {self.new_analyses_attempted} | "
            f"circuit breaker tripped: {self.circuit_breaker_tripped}"
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
