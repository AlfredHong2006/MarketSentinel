from datetime import UTC, datetime, timedelta

import pytest

from marketsentinel.historical_backfill import (
    BackfillBucket,
    BackfillBucketReport,
    BackfillRunReport,
    plan_backfill_buckets,
)


def test_buckets_are_calendar_months_oldest_first_and_cover_the_full_horizon() -> None:
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    horizon_start = now - timedelta(days=366)

    buckets = plan_backfill_buckets(now, horizon_days=366)

    # The first bucket is clipped to the exact horizon start (never the whole containing month),
    # and the last bucket is clipped to `now` (never runs past it).
    assert buckets[0].start == horizon_start
    assert buckets[0].label == f"{horizon_start.year:04d}-{horizon_start.month:02d}"
    assert buckets[-1].end == now
    assert buckets[-1].label == f"{now.year:04d}-{now.month:02d}"
    # Contiguous, gap-free, non-overlapping, and every bucket has positive width.
    for earlier, later in zip(buckets, buckets[1:], strict=False):
        assert earlier.end == later.start
        assert earlier.start < earlier.end
    # A ~366-day horizon spans 12 or 13 calendar months depending on day-of-month alignment.
    assert 12 <= len(buckets) <= 13


def test_buckets_never_extend_past_the_requested_horizon_or_now() -> None:
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    horizon_start = now - timedelta(days=90)

    buckets = plan_backfill_buckets(now, horizon_days=90)

    assert buckets[0].start == horizon_start
    assert buckets[-1].end == now
    assert all(horizon_start <= bucket.start < bucket.end <= now for bucket in buckets)


def test_zero_or_negative_horizon_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan_backfill_buckets(datetime(2026, 8, 21, tzinfo=UTC), horizon_days=0)


def test_a_horizon_landing_exactly_on_a_month_boundary_yields_no_zero_width_bucket() -> None:
    now = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

    buckets = plan_backfill_buckets(now, horizon_days=31)

    assert all(bucket.end > bucket.start for bucket in buckets)
    assert buckets[-1].end == now


def test_run_report_render_never_implies_full_coverage_when_a_bucket_failed() -> None:
    bucket = BackfillBucket(
        label="2026-03",
        start=datetime(2026, 3, 1, tzinfo=UTC),
        end=datetime(2026, 4, 1, tzinfo=UTC),
    )
    report = BackfillRunReport(
        ticker="NVDA",
        horizon_days=366,
        buckets=(
            BackfillBucketReport(bucket=bucket, fetch_status="failed", message="provider outage"),
        ),
        new_analyses_attempted=0,
        circuit_breaker_tripped=False,
        sentiment_dates_total=0,
    )

    rendered = report.render()

    assert "failed" in rendered
    assert "provider outage" in rendered
    assert "2026-03" in rendered
