"""CLI-level guarantees for the historical backfill script.

These cover the two things that are invisible from the service class alone: how ``--as-of`` is
parsed/restricted, and whether the offline maintenance mode really wires cache-only constituent
access into every collaborator that can reach the network.
"""

from datetime import UTC, datetime

import pytest

from marketsentinel.config import Settings
from marketsentinel.constituents import CacheOnlyConstituentResolver
from scripts.backfill_historical_intelligence import (
    build_backfill_service,
    horizon_days_for,
    parse_as_of,
)


def test_months_convert_to_the_backfill_horizon_the_audited_run_used() -> None:
    assert horizon_days_for(12) == 360


def test_as_of_requires_an_explicit_utc_offset() -> None:
    with pytest.raises(ValueError, match="must include a UTC offset"):
        parse_as_of("2026-08-21T18:32:05.171073")


def test_as_of_preserves_the_pinned_instant_and_normalises_to_utc() -> None:
    assert parse_as_of("2026-08-21T18:32:05.171073+00:00") == datetime(
        2026, 8, 21, 18, 32, 5, 171073, tzinfo=UTC
    )
    assert parse_as_of("2026-08-21T19:32:05+01:00") == datetime(2026, 8, 21, 18, 32, 5, tzinfo=UTC)


def test_as_of_is_rejected_outside_the_fill_selection_gaps_mode() -> None:
    import scripts.backfill_historical_intelligence as cli

    argv = [
        "backfill_historical_intelligence.py",
        "--ticker",
        "NVDA",
        "--mode",
        "backfill",
        "--as-of",
        "2026-08-21T18:32:05+00:00",
    ]

    # The argv describes a real, paid backfill. Rejection must happen at argument validation;
    # if control ever reaches settings or service construction, fail loudly rather than letting
    # a test fall through into the configured database and live providers.
    def must_not_be_reached(*args, **kwargs):
        raise AssertionError("must exit before settings/DB work")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(SystemExit):
        patch.setattr("sys.argv", argv)
        patch.setattr(cli, "get_settings", must_not_be_reached)
        patch.setattr(cli, "build_backfill_service", must_not_be_reached)
        cli.main()


def _settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "market.db",
        constituent_cache_path=tmp_path / "constituents.json",
        llm_api_key=None,
    )


def test_offline_mode_wires_cache_only_resolution_into_service_and_stage_c(
    writable_tmp_path,
) -> None:
    service = build_backfill_service(
        _settings(writable_tmp_path),
        bucket_candidate_cap=5,
        max_new_analyses=60,
        offline=True,
    )

    assert isinstance(service.constituents, CacheOnlyConstituentResolver)
    # Stage C normalises related companies through its own constituent handle, so an offline
    # run is only genuinely offline when that handle is cache-only too.
    assert isinstance(service.article_analysis_runner.constituents, CacheOnlyConstituentResolver)


def test_ordinary_modes_keep_the_fetching_constituent_service(writable_tmp_path) -> None:
    service = build_backfill_service(
        _settings(writable_tmp_path),
        bucket_candidate_cap=5,
        max_new_analyses=60,
    )

    assert not isinstance(service.constituents, CacheOnlyConstituentResolver)
    assert not isinstance(
        service.article_analysis_runner.constituents, CacheOnlyConstituentResolver
    )
