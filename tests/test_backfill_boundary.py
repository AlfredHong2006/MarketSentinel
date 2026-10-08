"""MR-009: a backfill that ends exactly at the start of stored history, plan-only, the read-cap guard.

Fully offline: scripted news, a static sentiment backend, throwaway databases under
data/test-runtime/, and a socket guard around the plan-only path.
"""

import hashlib
import socket
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import make_article
from test_backfill_service import (
    NOW,
    BucketAwareHistoricalNews,
    _service,
    _store_scored,
)

from marketsentinel.aggregation.sentiment import aggregate_daily_sentiment
from marketsentinel.backfill_service import (
    ScoredReadCapExceeded,
    plan_anchored_backfill,
    resolve_anchored_boundary,
)
from marketsentinel.domain import NewsFetchResult
from marketsentinel.historical_backfill import (
    BackfillRefusal,
    plan_backfill_buckets,
    resolve_backfill_boundary,
)
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts import backfill_historical_intelligence as cli
from scripts.backfill_historical_intelligence import (
    ReadOnlySQLiteRepository,
    horizon_days_for,
    is_single_ticker,
    parse_until,
)

HORIZON = horizon_days_for(36)  # 1080
BOUNDARY = datetime(2025, 9, 14, tzinfo=UTC)  # mid-month, so the bucket holding it is clipped


def _repository(tmp_path) -> SQLiteRepository:
    repository = SQLiteRepository(tmp_path / "market.db")
    repository.initialize()
    return repository


def _seed_history(
    repository: SQLiteRepository, start: datetime, count: int = 4, step_days: int = 1
):
    """Stored history beginning at ``start`` (the first article is published exactly at it)."""

    articles = [
        make_article(
            title=f"Acme stored history {index}",
            published_at=start + timedelta(days=index * step_days),
            url=f"https://stored.example/{index}",
            source=f"Stored Wire {index % 2}",
        )
        for index in range(count)
    ]
    _store_scored(repository, articles)
    return articles


class InclusiveEdgeNews(BucketAwareHistoricalNews):
    """Bounds its window inclusively like the real providers, and misbehaves past it."""

    def fetch_history(self, constituent, since, until, max_articles) -> NewsFetchResult:
        result = super().fetch_history(constituent, since, until, max_articles)
        extras = [
            make_article(
                title=f"Acme edge article {until.isoformat()} {offset}",
                published_at=until + timedelta(days=offset),
                url=f"https://edge.example/{until.isoformat()}-{offset}",
                source="Edge Wire",
            )
            for offset in (0, 1)  # exactly at the end of the window, and a day beyond it
        ]
        return NewsFetchResult(
            articles=[*result.articles, *extras], health=result.health, funnel=result.funnel
        )


# --- the planner ------------------------------------------------------------------------------


def test_an_anchored_plan_keeps_every_earlier_boundary_of_a_plain_run() -> None:
    plain = plan_backfill_buckets(NOW, horizon_days=HORIZON)
    anchored = plan_backfill_buckets(NOW, horizon_days=HORIZON, until=BOUNDARY)

    assert anchored[:-1] == plain[: len(anchored) - 1]
    assert anchored[-1].label == "2025-09"
    assert anchored[-1].start == datetime(2025, 9, 1, tzinfo=UTC)
    assert anchored[-1].end == BOUNDARY < plain[len(anchored) - 1].end
    assert anchored[0].start == NOW - timedelta(days=HORIZON)
    for earlier, later in zip(anchored, anchored[1:], strict=False):
        assert earlier.end == later.start


def test_a_boundary_on_a_month_start_never_yields_a_zero_width_bucket() -> None:
    boundary = datetime(2025, 9, 1, tzinfo=UTC)

    buckets = plan_backfill_buckets(NOW, horizon_days=HORIZON, until=boundary)

    assert buckets[-1].label == "2025-08"
    assert buckets[-1].end == boundary
    assert all(bucket.end > bucket.start for bucket in buckets)


def test_with_no_boundary_the_plan_is_what_it_was_before() -> None:
    for horizon in (30, 360, 1080):
        assert plan_backfill_buckets(NOW, horizon, 0) == plan_backfill_buckets(
            NOW, horizon, 0, None
        )


@pytest.mark.parametrize(
    "until",
    [NOW - timedelta(days=HORIZON), NOW - timedelta(days=HORIZON + 1), NOW + timedelta(days=1)],
)
def test_a_boundary_outside_the_horizon_is_rejected_by_the_planner(until: datetime) -> None:
    with pytest.raises(ValueError, match="until"):
        plan_backfill_buckets(NOW, horizon_days=HORIZON, until=until)


def test_a_boundary_and_an_offset_are_mutually_exclusive_in_the_planner() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        plan_backfill_buckets(NOW, horizon_days=HORIZON, offset_days=360, until=BOUNDARY)


# --- resolving the boundary ---------------------------------------------------------------------


def test_the_boundary_is_the_earliest_stored_non_demo_article(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, BOUNDARY)
    demo = make_article(
        title="Acme demo article", published_at=BOUNDARY - timedelta(days=200)
    ).model_copy(update={"is_demo": True})
    _store_scored(repository, [demo])

    resolved = resolve_anchored_boundary(repository, "acme", now=NOW, horizon_days=HORIZON)

    assert resolved == BOUNDARY


def test_an_explicit_boundary_wins_over_the_stored_start(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, BOUNDARY)
    override = BOUNDARY - timedelta(days=10)

    resolved = resolve_anchored_boundary(
        repository, "ACME", now=NOW, horizon_days=HORIZON, override=override
    )

    assert resolved == override


def test_an_empty_corpus_is_refused(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)

    with pytest.raises(BackfillRefusal, match="no stored non-demo articles"):
        resolve_anchored_boundary(repository, "ACME", now=NOW, horizon_days=HORIZON)


@pytest.mark.parametrize(
    "stored_start",
    [NOW - timedelta(days=HORIZON + 5), NOW - timedelta(days=HORIZON), NOW + timedelta(days=2)],
)
def test_a_stored_start_outside_the_horizon_is_refused(stored_start: datetime) -> None:
    with pytest.raises(BackfillRefusal, match="outside the horizon"):
        resolve_backfill_boundary(
            ticker="ACME",
            stored_start=stored_start,
            override=None,
            now=NOW,
            horizon_days=HORIZON,
        )


# --- the run ends exactly at the boundary -------------------------------------------------------


def test_a_run_ends_exactly_at_the_boundary_and_stores_nothing_at_or_after_it(
    writable_tmp_path,
) -> None:
    repository = _repository(writable_tmp_path)
    seeded = _seed_history(repository, BOUNDARY)
    news = InclusiveEdgeNews(articles_per_bucket=3)
    service, provider = _service(repository, news, max_new_analyses=0)

    report = service.backfill("ACME", now=NOW, horizon_days=HORIZON, until=BOUNDARY)

    expected = plan_backfill_buckets(NOW, horizon_days=HORIZON, until=BOUNDARY)
    assert news.calls == [(bucket.start, bucket.end) for bucket in expected]
    assert news.calls[-1][1] == BOUNDARY, "the bucket holding the boundary is clipped there"
    stored = repository.list_articles("ACME", since=None)
    at_or_after = {item.fingerprint for item in stored if item.published_at >= BOUNDARY}
    assert at_or_after == {item.fingerprint for item in seeded}, "only the pre-existing history"
    assert any(
        item.published_at < BOUNDARY for item in stored if item.url.startswith("https://history")
    )
    assert report.until == BOUNDARY
    assert "ending exactly at 2025-09-14T00:00:00+00:00" in report.render().splitlines()[0]
    assert provider.total_calls == 0


def test_the_edge_articles_of_earlier_buckets_are_kept_and_only_the_final_edge_is_dropped(
    writable_tmp_path,
) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, BOUNDARY)
    news = InclusiveEdgeNews(articles_per_bucket=1)
    service, _provider = _service(repository, news, max_new_analyses=0)

    service.backfill("ACME", now=NOW, horizon_days=HORIZON, until=BOUNDARY)

    titles = {item.title for item in repository.list_articles("ACME", since=None)}
    first_bucket_end = plan_backfill_buckets(NOW, HORIZON, until=BOUNDARY)[0].end
    assert f"Acme edge article {first_bucket_end.isoformat()} 0" in titles
    assert f"Acme edge article {BOUNDARY.isoformat()} 0" not in titles
    assert f"Acme edge article {BOUNDARY.isoformat()} 1" not in titles


def test_rerunning_an_anchored_run_adds_no_rows(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, BOUNDARY)
    news = InclusiveEdgeNews(articles_per_bucket=2)
    service, _provider = _service(repository, news, max_new_analyses=0)

    service.backfill("ACME", now=NOW, horizon_days=HORIZON, until=BOUNDARY)
    first = len(repository.list_articles("ACME", since=None))
    service.backfill("ACME", now=NOW, horizon_days=HORIZON, until=BOUNDARY)

    assert len(repository.list_articles("ACME", since=None)) == first


def test_a_boundary_outside_the_horizon_fails_before_any_fetch(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    news = BucketAwareHistoricalNews()
    service, _provider = _service(repository, news, max_new_analyses=0)

    with pytest.raises(ValueError, match="until"):
        service.backfill(
            "ACME", now=NOW, horizon_days=HORIZON, until=NOW - timedelta(days=HORIZON + 10)
        )

    assert news.calls == []


# --- the CLI ------------------------------------------------------------------------------------


def _run_cli(monkeypatch, tmp_path, argv, service=None, build_calls=None):
    """Run ``cli.main`` with settings pointed at ``tmp_path/market.db`` and a given service."""

    def fake_build(settings, **kwargs):
        if build_calls is not None:
            build_calls.append(kwargs)
        if service is None:
            raise AssertionError("must not build a service")
        return service

    monkeypatch.setattr(
        "sys.argv", ["backfill_historical_intelligence.py", "--ticker", "ACME", *argv]
    )
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(database_path=tmp_path / "market.db")
    )
    monkeypatch.setattr(cli, "build_backfill_service", fake_build)
    cli.main()


def _exit_code(monkeypatch, tmp_path, argv, **kwargs) -> int:
    with pytest.raises(SystemExit) as raised:
        _run_cli(monkeypatch, tmp_path, argv, **kwargs)
    return raised.value.code


def test_the_cli_runs_to_the_stored_start_and_passes_the_five_arguments(
    monkeypatch, writable_tmp_path, capsys
) -> None:
    repository = _repository(writable_tmp_path)
    now = datetime.now(UTC)
    # Mid-month, about a year back: far from a month start, so the edge articles of the previous
    # bucket (one day past its end) cannot land at or after the boundary whatever today's date is.
    start = datetime(now.year - 1, now.month, 15, 6, tzinfo=UTC)
    seeded = _seed_history(repository, start)
    news = InclusiveEdgeNews(articles_per_bucket=2)
    service, _provider = _service(repository, news, max_new_analyses=0)
    build_calls: list[dict] = []

    _run_cli(
        monkeypatch,
        writable_tmp_path,
        [
            "--months",
            "36",
            "--until-stored-start",
            "--google-only",
            "--max-new-analyses",
            "0",
            "--request-interval-seconds",
            "5.25",
        ],
        service=service,
        build_calls=build_calls,
    )

    assert build_calls[0]["google_only"] is True
    assert build_calls[0]["max_new_analyses"] == 0
    assert build_calls[0]["request_interval_seconds"] == 5.25
    assert news.calls[-1][1] == start
    stored = repository.list_articles("ACME", since=None)
    assert {i.fingerprint for i in stored if i.published_at >= start} == {
        i.fingerprint for i in seeded
    }
    assert f"nothing at or after {start.isoformat()} will be stored" in capsys.readouterr().out


def test_the_cli_refuses_an_empty_corpus_before_any_fetch(
    monkeypatch, writable_tmp_path, capsys
) -> None:
    repository = _repository(writable_tmp_path)
    news = BucketAwareHistoricalNews()
    service, _provider = _service(repository, news, max_new_analyses=0)

    code = _exit_code(
        monkeypatch,
        writable_tmp_path,
        ["--months", "36", "--until-stored-start", "--google-only"],
        service=service,
    )

    assert code == 2
    assert news.calls == []
    assert "no stored non-demo articles" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("stored_days_ago", "extra"),
    [
        (1100, ["--until-stored-start"]),  # stored history already reaches past 36 months
        (5, ["--until", "2999-01-01T00:00:00+00:00"]),  # an override in the future
    ],
)
def test_the_cli_refuses_a_boundary_outside_the_horizon_before_any_fetch(
    monkeypatch, writable_tmp_path, capsys, stored_days_ago: int, extra: list[str]
) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, datetime.now(UTC) - timedelta(days=stored_days_ago))
    news = BucketAwareHistoricalNews()
    service, _provider = _service(repository, news, max_new_analyses=0)

    code = _exit_code(monkeypatch, writable_tmp_path, ["--months", "36", *extra], service=service)

    assert code == 2
    assert news.calls == []
    assert "outside the horizon" in capsys.readouterr().err


def test_the_cli_refuses_a_missing_database_without_creating_one(
    monkeypatch, writable_tmp_path, capsys
) -> None:
    code = _exit_code(
        monkeypatch, writable_tmp_path, ["--months", "36", "--until-stored-start", "--plan-only"]
    )

    assert code == 2
    assert not (writable_tmp_path / "market.db").exists()
    assert "no database at" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--until-stored-start", "--skip-recent-months", "12", "--months", "36"],
        ["--until-stored-start", "--until", "2025-09-14T00:00:00+00:00"],
        ["--until", "2025-09-14T00:00:00", "--months", "36"],  # no UTC offset
        ["--plan-only"],  # needs a boundary option
        ["--mode", "reanalyze-stale", "--until-stored-start"],
        ["--mode", "fill-selection-gaps", "--plan-only", "--until-stored-start"],
    ],
)
def test_inconsistent_boundary_options_are_refused_before_any_settings_or_database_work(
    extra: list[str],
) -> None:
    def must_not_be_reached(*args, **kwargs):
        raise AssertionError("must exit before settings/DB work")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(SystemExit):
        patch.setattr(
            "sys.argv", ["backfill_historical_intelligence.py", "--ticker", "NVDA", *extra]
        )
        patch.setattr(cli, "get_settings", must_not_be_reached)
        patch.setattr(cli, "build_backfill_service", must_not_be_reached)
        cli.main()


@pytest.mark.parametrize("ticker", ["NVDA,PFE", "NVDA PFE", "NVDA, PFE", "", ",NVDA"])
def test_the_cli_takes_exactly_one_ticker(ticker: str) -> None:
    def must_not_be_reached(*args, **kwargs):
        raise AssertionError("must exit before settings/DB work")

    with pytest.MonkeyPatch.context() as patch, pytest.raises(SystemExit):
        patch.setattr("sys.argv", ["backfill_historical_intelligence.py", "--ticker", ticker])
        patch.setattr(cli, "get_settings", must_not_be_reached)
        cli.main()


def test_ticker_and_timestamp_parsing() -> None:
    assert [is_single_ticker(value) for value in ("NVDA", "brk.b", "BF-B", "NVDA,PFE", "A B")] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert parse_until("2025-09-14T02:00:00+02:00") == BOUNDARY
    with pytest.raises(ValueError, match="UTC offset"):
        parse_until("2025-09-14T00:00:00")


# --- plan-only: no network, no write --------------------------------------------------------------


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("plan-only must not use the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _snapshot(directory) -> tuple[list[str], str]:
    names = sorted(path.name for path in directory.iterdir())
    return names, hashlib.sha256((directory / "market.db").read_bytes()).hexdigest()


def test_plan_only_prints_the_plan_and_makes_no_network_call_and_no_write(
    monkeypatch, writable_tmp_path, capsys, no_network
) -> None:
    repository = _repository(writable_tmp_path)
    now = datetime.now(UTC)
    start = (now - timedelta(days=400)).replace(microsecond=0)
    _seed_history(repository, start, count=7, step_days=5)  # 6 fall in the first 30 days
    _seed_history(repository, start + timedelta(days=200), count=2)
    older_demo = make_article(
        title="Acme demo", published_at=start - timedelta(days=50), url="https://demo.example/x"
    ).model_copy(update={"is_demo": True})
    _store_scored(repository, [older_demo])
    before = _snapshot(writable_tmp_path)

    _run_cli(
        monkeypatch,
        writable_tmp_path,
        ["--months", "36", "--until-stored-start", "--google-only", "--plan-only"],
    )

    output = capsys.readouterr().out
    assert f"boundary: {start.isoformat()}" in output
    for index in range(5):
        assert (start + timedelta(days=index * 5)).isoformat() in output
    assert (start + timedelta(days=30)).isoformat() not in output.split("planned buckets")[0]
    assert "in the 30 days after the boundary: 6 (of 9 stored in total)" in output
    assert "planned buckets:" in output
    assert "nothing written" in output
    assert _snapshot(writable_tmp_path) == before


def test_plan_only_reads_through_a_connection_that_cannot_write(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, BOUNDARY)
    read_only = ReadOnlySQLiteRepository(writable_tmp_path / "market.db")

    assert len(read_only.list_articles("ACME", since=None)) == 4
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        read_only.upsert_articles([make_article(title="Acme late write", published_at=NOW)])
    assert len(repository.list_articles("ACME", since=None)) == 4


def test_the_plan_names_the_oldest_stored_values_so_an_isolated_early_article_shows(
    writable_tmp_path,
) -> None:
    """One stray old article pins the boundary early and hides the real, denser start."""

    repository = _repository(writable_tmp_path)
    stray = BOUNDARY - timedelta(days=200)
    _store_scored(repository, [make_article(title="Acme stray", published_at=stray)])
    _seed_history(repository, BOUNDARY, count=6)

    plan = plan_anchored_backfill(repository, "ACME", now=NOW, horizon_days=HORIZON)

    assert plan.boundary == stray
    assert plan.earliest_published[:2] == (stray, BOUNDARY)
    assert plan.stored_in_30_days_after_boundary == 1, "far fewer than a normal month"
    rendered = plan.render()
    assert stray.isoformat() in rendered and BOUNDARY.isoformat() in rendered


# --- the read-cap guard -----------------------------------------------------------------------------

CAP = 5


def _capped_service(repository, news):
    service, _provider = _service(repository, news, max_new_analyses=0)
    service.scored_read_cap = CAP
    return service


def _seed_recent_scored(repository, count: int):
    return _seed_history(repository, NOW - timedelta(days=20), count=count)


def test_a_corpus_that_already_fills_the_cap_is_refused_before_any_fetch(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    _seed_recent_scored(repository, CAP)  # exactly at the cap: the read could be truncated
    news = BucketAwareHistoricalNews(articles_per_bucket=1)
    service = _capped_service(repository, news)

    with pytest.raises(BackfillRefusal, match="read cap"):
        service.backfill("ACME", now=NOW, horizon_days=60)

    assert news.calls == []
    assert len(repository.list_articles("ACME", since=None)) == CAP
    assert repository.list_daily_sentiment("ACME") == []


def test_a_corpus_one_below_the_cap_is_read_in_full_and_the_run_completes(
    writable_tmp_path,
) -> None:
    repository = _repository(writable_tmp_path)
    _seed_recent_scored(repository, CAP - 1)
    news = BucketAwareHistoricalNews(articles_per_bucket=0)
    service = _capped_service(repository, news)

    report = service.backfill("ACME", now=NOW, horizon_days=60)

    assert news.calls, "the run was allowed to fetch"
    assert report.sentiment_dates_total > 0
    assert repository.list_daily_sentiment("ACME")


def test_storing_past_the_cap_stops_the_run_and_leaves_daily_sentiment_untouched(
    writable_tmp_path,
) -> None:
    repository = _repository(writable_tmp_path)
    seeded = _seed_recent_scored(repository, CAP - 1)
    repository.upsert_daily_sentiment(
        aggregate_daily_sentiment("ACME", repository.list_scored_articles("ACME"))
    )
    before = repository.list_daily_sentiment("ACME")
    assert before
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service = _capped_service(repository, news)

    with pytest.raises(ScoredReadCapExceeded) as raised:
        service.backfill("ACME", now=NOW, horizon_days=60)

    assert raised.value.cap == CAP
    assert "daily_sentiment was not rewritten" in str(raised.value)
    assert repository.list_daily_sentiment("ACME") == before
    stored = repository.list_articles("ACME", since=None)
    assert len(stored) > len(seeded), "what was stored before the stop stays stored"
    assert len(news.calls) == 1, "the run stopped at the first bucket whose read was truncated"


def test_a_truncated_rebuild_read_is_never_written(writable_tmp_path) -> None:
    """Each bucket's own read fits the cap; only the whole-horizon rebuild read overflows."""

    cap = 7
    repository = _repository(writable_tmp_path)
    _store_scored(
        repository,
        [
            make_article(
                title=f"Acme earlier {index}",
                published_at=datetime(2026, 7, 15, tzinfo=UTC) + timedelta(days=index),
                url=f"https://earlier.example/{index}",
            )
            for index in range(2)
        ],
    )
    repository.upsert_daily_sentiment(
        aggregate_daily_sentiment("ACME", repository.list_scored_articles("ACME"))
    )
    before = repository.list_daily_sentiment("ACME")
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service, _provider = _service(repository, news, max_new_analyses=0)
    service.scored_read_cap = cap

    with pytest.raises(ScoredReadCapExceeded) as raised:
        service.backfill("ACME", now=NOW, horizon_days=40)

    assert len(news.calls) == 2, "every planned bucket was fetched first"
    assert "for the daily-sentiment rebuild" in str(raised.value)
    assert "NOT deleted or rewritten" in str(raised.value)
    assert repository.list_daily_sentiment("ACME") == before
    assert len(repository.list_articles("ACME", since=None)) == 8


def test_the_selection_gap_fill_also_refuses_a_truncated_read(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    _seed_recent_scored(repository, CAP + 1)
    service = _capped_service(repository, BucketAwareHistoricalNews())

    with pytest.raises(ScoredReadCapExceeded):
        service.fill_selection_gaps("ACME", now=NOW, horizon_days=60)


def test_the_default_cap_is_the_unchanged_aggregation_limit(writable_tmp_path) -> None:
    repository = _repository(writable_tmp_path)
    service, _provider = _service(repository, BucketAwareHistoricalNews(), max_new_analyses=0)

    assert service.scored_read_cap == 5_000


def test_the_cli_reports_a_cap_stop_prominently_and_exits_non_zero(
    monkeypatch, writable_tmp_path, capsys
) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, datetime.now(UTC) - timedelta(days=10), count=CAP - 1)
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service = _capped_service(repository, news)

    code = _exit_code(
        monkeypatch,
        writable_tmp_path,
        ["--months", "2", "--max-new-analyses", "0"],
        service=service,
    )

    error = capsys.readouterr().err
    assert code == 3
    assert "BACKFILL STOPPED: SCORED-READ CAP EXCEEDED" in error
    assert "daily_sentiment was not rewritten" in error
    assert "database in R2 is unchanged" in error


def test_the_cli_exits_2_when_the_corpus_already_fills_the_cap(
    monkeypatch, writable_tmp_path, capsys
) -> None:
    repository = _repository(writable_tmp_path)
    _seed_history(repository, datetime.now(UTC) - timedelta(days=10), count=CAP)
    news = BucketAwareHistoricalNews(articles_per_bucket=3)
    service = _capped_service(repository, news)

    code = _exit_code(
        monkeypatch,
        writable_tmp_path,
        ["--months", "2", "--max-new-analyses", "0"],
        service=service,
    )

    assert code == 2
    assert news.calls == []
    assert "Nothing was fetched or written" in capsys.readouterr().err
