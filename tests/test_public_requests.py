"""Shared public requests: asking never spends, and the worker admits under hard caps.

Offline and deterministic: the request store is a directory (or a fake S3 client), the LLM
provider is scripted, and no network is reached. The two guarantees under test are that a public
request can only ever *ask* -- it writes one small idempotent object and nothing else -- and that
the worker's admission pass consumes exactly what it processed, under caps, so a stranger cannot
create unbounded work or spend.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_article
from fastapi.testclient import TestClient
from test_coverage_ledger import T0, ScriptedProvider, article, build
from test_overview import (
    COMPATIBILITY,
    FakePrices,
    read_only_service,
    seed_repository,
    stored_row_counts,
)
from test_public_mode import ExplodingArticleEvents, UniverseConstituents

from marketsentinel.api.app import Services, build_request_sink, create_app
from marketsentinel.config import Settings
from marketsentinel.domain import CapabilitiesView
from marketsentinel.errors import ConstituentNotFoundError
from marketsentinel.public_requests import (
    DirectoryRequestSink,
    PublicRequestService,
    R2RequestSink,
    RequestLimits,
    RequestSinkError,
    SlidingWindowRateLimiter,
    TickerCycleOrder,
    admit_public_requests,
    article_request_key,
    coverage_request_key,
    order_tickers_for_cycle,
    parse_request_key,
)
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts.run_coverage_cycle import build_parser, main, validate_arguments

PUBLIC = Settings(
    _env_file=None,
    public_mode=True,
    public_prepared_companies=("ACME",),
    public_default_symbol="ACME",
)

LIMITS = RequestLimits(
    rate_limit_per_minute=100,
    max_pending_coverage=3,
    max_pending_articles=2,
    max_covered_companies=10,
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _service(repository, sink, limits=LIMITS, clock=None) -> PublicRequestService:
    return PublicRequestService(
        sink=sink,
        repository=repository,
        constituents=UniverseConstituents(),
        compatibility=COMPATIBILITY,
        limits=limits,
        clock=clock or Clock(datetime(2026, 9, 13, 12, 0, tzinfo=UTC)),
    )


def _app(repository, requests: PublicRequestService | None, settings: Settings = PUBLIC):
    service = read_only_service(repository, FakePrices())
    service.constituents = UniverseConstituents()
    return create_app(
        settings=settings,
        services=Services(
            repository=repository,
            constituents=UniverseConstituents(),
            analysis=service,
            article_events=ExplodingArticleEvents(),
            requests=requests,
        ),
    )


# --------------------------------------------------------------------------------------------
# Keys and sinks
# --------------------------------------------------------------------------------------------


def test_keys_round_trip_and_anything_else_is_rejected() -> None:
    assert parse_request_key(coverage_request_key("BRK.B")).ticker == "BRK.B"
    parsed = parse_request_key(article_request_key("BT.A", "abc123"))
    assert (parsed.kind, parsed.ticker, parsed.article_id) == ("article", "BT.A", "abc123")
    for bad in (
        "coverage/nvda.json",
        "coverage/NVDA",
        "coverage/../etc.json",
        "articles/NVDA.json",
        "articles/NVDA/a/b.json",
        "articles/NVDA/has space.json",
        "other/NVDA.json",
    ):
        assert parse_request_key(bad) is None, bad


def test_directory_sink_writes_lists_and_reads_in_the_shared_layout(writable_tmp_path) -> None:
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    assert sink.list_keys() == []
    sink.put(coverage_request_key("ACME"), {"kind": "coverage", "ticker": "ACME"})
    sink.put(article_request_key("ACME", "f1"), {"kind": "article"})
    sink.put(coverage_request_key("ACME"), {"kind": "coverage", "ticker": "ACME", "again": 1})

    assert sink.list_keys() == ["articles/ACME/f1.json", "coverage/ACME.json"]
    assert sink.read("coverage/ACME.json") == {"again": 1, "kind": "coverage", "ticker": "ACME"}
    assert sink.read("coverage/MISSING.json") is None
    (writable_tmp_path / "requests" / "coverage" / "BAD.json").write_text("[]", encoding="utf-8")
    assert sink.read("coverage/BAD.json") is None


class FakeS3:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.objects: dict[str, bytes] = {}
        self.puts: list[dict] = []

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("boom")
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        objects = self.objects
        fail = self.fail

        class Paginator:
            def paginate(self, *, Bucket, Prefix):
                if fail:
                    raise RuntimeError("boom")
                keys = sorted(key for key in objects if key.startswith(Prefix))
                # Two pages, to prove pagination is followed.
                yield {"Contents": [{"Key": key} for key in keys[:1]]}
                yield {"Contents": [{"Key": key} for key in keys[1:]]}

        return Paginator()


def test_r2_sink_puts_json_objects_and_lists_both_prefixes() -> None:
    client = FakeS3()
    sink = R2RequestSink(
        bucket="requests", endpoint_url="https://r2.test", access_key_id="k",
        secret_access_key="s", client=client,
    )  # fmt: skip
    sink.put(coverage_request_key("ACME"), {"kind": "coverage"})
    sink.put(article_request_key("ACME", "f1"), {"kind": "article"})
    sink.put(article_request_key("ACME", "f2"), {"kind": "article"})

    assert client.puts[0]["Bucket"] == "requests"
    assert client.puts[0]["ContentType"] == "application/json"
    assert client.puts[0]["Body"] == b'{"kind": "coverage"}'
    assert sink.list_keys() == [
        "coverage/ACME.json",
        "articles/ACME/f1.json",
        "articles/ACME/f2.json",
    ]


def test_r2_sink_failures_are_reported_as_sink_errors() -> None:
    sink = R2RequestSink(
        bucket="requests", endpoint_url="https://r2.test", access_key_id="k",
        secret_access_key="s", client=FakeS3(fail=True),
    )  # fmt: skip
    with pytest.raises(RequestSinkError):
        sink.put("coverage/ACME.json", {})
    with pytest.raises(RequestSinkError):
        sink.list_keys()


def test_build_request_sink_reflects_configuration(writable_tmp_path) -> None:
    assert build_request_sink(Settings(_env_file=None)) is None
    directory = build_request_sink(
        Settings(_env_file=None, public_requests_directory=writable_tmp_path / "q")
    )
    assert isinstance(directory, DirectoryRequestSink)
    with pytest.raises(RuntimeError, match="missing"):
        build_request_sink(Settings(_env_file=None, public_requests_bucket="requests"))
    bucket = build_request_sink(
        Settings(
            _env_file=None,
            public_requests_bucket="requests",
            public_requests_endpoint_url="https://acct.r2.cloudflarestorage.com",
            public_requests_access_key_id="k",
            public_requests_secret_access_key="s",
        )
    )
    assert isinstance(bucket, R2RequestSink)


def test_sliding_window_rate_limiter_bounds_a_minute_and_then_recovers() -> None:
    limiter = SlidingWindowRateLimiter(2)
    start = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    assert limiter.allow(start)
    assert limiter.allow(start + timedelta(seconds=10))
    assert not limiter.allow(start + timedelta(seconds=20))
    assert limiter.allow(start + timedelta(seconds=61))


# --------------------------------------------------------------------------------------------
# The public side: asking never spends
# --------------------------------------------------------------------------------------------


def test_a_coverage_request_is_recorded_once_and_visible_to_everyone(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    before = stored_row_counts(repository)

    with TestClient(_app(repository, _service(repository, sink))) as client:
        first = client.post("/api/v1/companies/hidden3/coverage-requests")
        second = client.post("/api/v1/companies/HIDDEN3/coverage-requests")
        capabilities = CapabilitiesView.model_validate(client.get("/api/v1/capabilities").json())

    assert first.status_code == 202
    assert first.json()["state"] == "queued"
    assert first.json()["symbol"] == "HIDDEN3"
    assert second.json()["state"] == "already_queued"
    assert sink.list_keys() == ["coverage/HIDDEN3.json"]
    payload = sink.read("coverage/HIDDEN3.json")
    assert payload == {
        "kind": "coverage",
        "ticker": "HIDDEN3",
        "requested_at": "2026-09-13T12:00:00+00:00",
    }
    assert capabilities.supports_coverage_requests is True
    assert capabilities.pending_coverage_requests == ["HIDDEN3"]
    # The request store is the only thing written: the public database is untouched.
    assert stored_row_counts(repository) == before


def test_an_already_covered_company_is_an_honest_no_op(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    repository.activate_company_coverage("ACME", started_at=T0, live_window_days=30)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")

    with TestClient(_app(repository, _service(repository, sink))) as client:
        response = client.post("/api/v1/companies/ACME/coverage-requests")
        capabilities = client.get("/api/v1/capabilities").json()

    assert response.json()["state"] == "covered"
    assert sink.list_keys() == []
    assert capabilities["covered_companies"] == ["ACME"]


def test_an_unknown_company_cannot_be_requested(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    with TestClient(_app(repository, _service(repository, sink))) as client:
        assert client.post("/api/v1/companies/NOPE/coverage-requests").status_code == 404
    assert sink.list_keys() == []


def test_pending_queue_and_capacity_caps_refuse_with_429(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    with TestClient(_app(repository, _service(repository, sink))) as client:
        for index in range(3):
            assert (
                client.post(f"/api/v1/companies/HIDDEN{index}/coverage-requests").status_code == 202
            )
        overflow = client.post("/api/v1/companies/HIDDEN9/coverage-requests")
    assert overflow.status_code == 429
    assert "queue is full" in overflow.json()["detail"]
    assert len(sink.list_keys()) == 3

    # Capacity counts active coverage too: one active company + a cap of 1 leaves no room.
    repository.activate_company_coverage("ACME", started_at=T0, live_window_days=30)
    tight = RequestLimits(
        rate_limit_per_minute=100,
        max_pending_coverage=10,
        max_pending_articles=10,
        max_covered_companies=1,
    )
    empty_sink = DirectoryRequestSink(writable_tmp_path / "requests2")
    with TestClient(_app(repository, _service(repository, empty_sink, tight))) as client:
        response = client.post("/api/v1/companies/HIDDEN1/coverage-requests")
    assert response.status_code == 429
    assert "capacity" in response.json()["detail"]
    assert empty_sink.list_keys() == []


def test_the_process_wide_rate_limit_applies_to_both_request_kinds(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    unanalysed = make_article(title="An unanalysed Acme story", url="https://example.com/u1")
    repository.upsert_articles([unanalysed])
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    limits = RequestLimits(
        rate_limit_per_minute=1,
        max_pending_coverage=10,
        max_pending_articles=10,
        max_covered_companies=10,
    )
    clock = Clock(datetime(2026, 9, 13, 12, 0, tzinfo=UTC))
    with TestClient(_app(repository, _service(repository, sink, limits, clock))) as client:
        assert client.post("/api/v1/companies/HIDDEN1/coverage-requests").status_code == 202
        limited = client.post(
            f"/api/v1/companies/ACME/articles/{unanalysed.fingerprint}/analysis-requests"
        )
        assert limited.status_code == 429
        clock.now += timedelta(minutes=2)
        recovered = client.post(
            f"/api/v1/companies/ACME/articles/{unanalysed.fingerprint}/analysis-requests"
        )
    assert recovered.status_code == 202
    assert sorted(sink.list_keys()) == [
        f"articles/ACME/{unanalysed.fingerprint}.json",
        "coverage/HIDDEN1.json",
    ]


def test_article_requests_validate_against_the_stored_corpus(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    analysed = repository.list_articles("ACME")[0]
    unanalysed = make_article(title="An unanalysed Acme story", url="https://example.com/u1")
    demo = make_article(title="A demo Acme story", url="https://example.com/d1").model_copy(
        update={"is_demo": True}
    )
    repository.upsert_articles([unanalysed, demo])
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    before = stored_row_counts(repository)

    with TestClient(_app(repository, _service(repository, sink))) as client:
        base = "/api/v1/companies/ACME/articles"
        missing = client.post(f"{base}/no-such-article/analysis-requests")
        wrong_company = client.post(
            f"/api/v1/companies/HIDDEN1/articles/{unanalysed.fingerprint}/analysis-requests"
        )
        already = client.post(f"{base}/{analysed.fingerprint}/analysis-requests")
        demo_response = client.post(f"{base}/{demo.fingerprint}/analysis-requests")
        queued = client.post(f"{base}/{unanalysed.fingerprint}/analysis-requests")
        again = client.post(f"{base}/{unanalysed.fingerprint}/analysis-requests")
        capabilities = client.get("/api/v1/capabilities").json()

    assert missing.status_code == 404
    assert wrong_company.status_code == 404
    assert (already.status_code, already.json()["state"]) == (202, "analysed")
    assert demo_response.status_code == 409
    assert (queued.status_code, queued.json()["state"]) == (202, "queued")
    assert again.json()["state"] == "already_queued"
    assert sink.list_keys() == [f"articles/ACME/{unanalysed.fingerprint}.json"]
    assert capabilities["pending_article_requests"] == [unanalysed.fingerprint]
    assert stored_row_counts(repository) == before


def test_the_pending_article_cap_refuses_with_429(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    items = [
        make_article(title=f"Unanalysed Acme story {index}", url=f"https://example.com/u{index}")
        for index in range(3)
    ]
    repository.upsert_articles(items)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    with TestClient(_app(repository, _service(repository, sink))) as client:
        statuses = [
            client.post(
                f"/api/v1/companies/ACME/articles/{item.fingerprint}/analysis-requests"
            ).status_code
            for item in items
        ]
    assert statuses == [202, 202, 429]
    assert len(sink.list_keys()) == 2


def test_without_a_request_store_the_surface_does_not_exist(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    with TestClient(_app(repository, None)) as client:
        capabilities = CapabilitiesView.model_validate(client.get("/api/v1/capabilities").json())
        coverage = client.post("/api/v1/companies/HIDDEN1/coverage-requests")
        analysis = client.post("/api/v1/companies/ACME/articles/x/analysis-requests")
    assert capabilities.supports_coverage_requests is False
    assert capabilities.pending_coverage_requests == []
    assert coverage.status_code == 404
    assert analysis.status_code == 404


def test_the_pending_mirror_is_rebuilt_from_the_store_at_startup(writable_tmp_path) -> None:
    """The public host's disk is ephemeral; what is queued must survive a restart."""

    repository = seed_repository(writable_tmp_path)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    sink.put(coverage_request_key("HIDDEN7"), {"kind": "coverage", "ticker": "HIDDEN7"})
    sink.put(article_request_key("ACME", "abc"), {"kind": "article"})
    (writable_tmp_path / "requests" / "coverage" / "junk.txt").write_text("x", encoding="utf-8")

    with TestClient(_app(repository, _service(repository, sink))) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        repeat = client.post("/api/v1/companies/HIDDEN7/coverage-requests")

    assert capabilities["pending_coverage_requests"] == ["HIDDEN7"]
    assert capabilities["pending_article_requests"] == ["abc"]
    assert repeat.json()["state"] == "already_queued"


def test_a_store_failure_is_a_503_and_records_nothing(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)

    class BrokenSink:
        def put(self, key, payload):
            raise RequestSinkError("down")

        def list_keys(self):
            raise RequestSinkError("down")

    with TestClient(_app(repository, _service(repository, BrokenSink()))) as client:
        response = client.post("/api/v1/companies/HIDDEN1/coverage-requests")
        capabilities = client.get("/api/v1/capabilities").json()
    assert response.status_code == 503
    assert capabilities["pending_coverage_requests"] == []


def test_the_spending_endpoints_stay_closed_in_public_mode_alongside_requests(
    writable_tmp_path,
) -> None:
    repository = seed_repository(writable_tmp_path)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    with TestClient(_app(repository, _service(repository, sink))) as client:
        assert client.post("/api/v1/analyze", json={"symbol": "ACME"}).status_code == 404
        assert client.post("/api/v1/articles/analyze", json={"article_id": "x"}).status_code == 404


# --------------------------------------------------------------------------------------------
# The worker side: admission under hard caps, consuming exactly what it processed
# --------------------------------------------------------------------------------------------


class TwoCompanyConstituents:
    """Resolves ACME and OTHER; anything else is not in the universe."""

    def resolve(self, symbol: str):
        from conftest import make_constituent

        if symbol == "ACME":
            return make_constituent()
        if symbol == "OTHER":
            return make_constituent().model_copy(
                update={"symbol": "OTHER", "yahoo_symbol": "OTHER"}
            )
        raise ConstituentNotFoundError(f"{symbol!r} is not in the available constituent universe")

    def load(self):
        from marketsentinel.domain import UniverseResult

        return UniverseResult(
            constituents=[self.resolve("ACME"), self.resolve("OTHER")],
            source="test",
            is_fallback=False,
            fetched_at=T0,
        )


def _worker(writable_tmp_path, provider=None):
    h = build(writable_tmp_path, provider=provider)
    h.cycle.constituents = TwoCompanyConstituents()
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    return h, sink


def _admit(h, sink, **caps):
    return admit_public_requests(
        sink=sink,
        coverage=h.cycle,
        runner=h.runner,
        repository=h.repository,
        now=T0,
        max_new_tickers=caps.get("max_new_tickers", 3),
        max_article_requests=caps.get("max_article_requests", 20),
    )


def test_coverage_requests_activate_under_a_cap_and_spend_nothing(writable_tmp_path) -> None:
    h, sink = _worker(writable_tmp_path)
    sink.put(coverage_request_key("OTHER"), {"requested_at": "2026-09-01T10:00:00+00:00"})
    sink.put(coverage_request_key("ACME"), {"requested_at": "2026-09-01T09:00:00+00:00"})
    sink.put(coverage_request_key("NOPE"), {"requested_at": "2026-09-01T08:00:00+00:00"})

    report = _admit(h, sink, max_new_tickers=1)

    # Oldest first: NOPE is rejected (and consumed), ACME activated, OTHER deferred by the cap.
    assert report.activated == ("ACME",)
    assert report.deferred_coverage == ("OTHER",)
    assert report.rejected == (
        ("coverage/NOPE.json", "'NOPE' is not in the available constituent universe"),
    )
    assert sorted(report.consumed_keys) == ["coverage/ACME.json", "coverage/NOPE.json"]
    assert h.repository.get_company_coverage("ACME").active is True
    assert h.repository.get_company_coverage("OTHER") is None
    assert h.provider.stage_a_calls == 0

    again = _admit(h, sink, max_new_tickers=1)
    assert again.already_covered == ("ACME",)
    assert again.activated == ("OTHER",)
    assert "Public request admission" in again.render()


def test_article_requests_run_through_the_ledger_under_a_cap(writable_tmp_path) -> None:
    h, sink = _worker(writable_tmp_path)
    items = [article(index, T0 - timedelta(hours=index)) for index in range(1, 4)]
    h.repository.upsert_articles(items)
    assert h.service.analyze_article(items[0].fingerprint).status == "generated"
    for index, item in enumerate(items):
        sink.put(
            article_request_key("ACME", item.fingerprint),
            {"requested_at": f"2026-09-01T0{index}:00:00+00:00"},
        )
    sink.put(article_request_key("ACME", "ghost"), {"requested_at": "2026-09-01T00:00:00+00:00"})
    sink.put(
        article_request_key("OTHER", items[1].fingerprint),
        {"requested_at": "2026-08-31T00:00:00+00:00"},
    )
    calls_before = h.provider.stage_a_calls

    report = _admit(h, sink, max_article_requests=2)

    # Reused (no pay), then one generated; the third is deferred by the cap; ghost and the
    # wrong-ticker key are rejected and consumed.
    assert [status for _, status in report.articles] == ["analyzed", "analyzed"]
    assert report.deferred_articles == (items[2].fingerprint,)
    assert {key for key, _ in report.rejected} == {
        "articles/ACME/ghost.json",
        f"articles/OTHER/{items[1].fingerprint}.json",
    }
    assert h.provider.stage_a_calls == calls_before + 1
    assert f"articles/ACME/{items[2].fingerprint}.json" not in report.consumed_keys
    assert f"articles/ACME/{items[1].fingerprint}.json" in report.consumed_keys
    assert h.job(items[1]).state == "analyzed"


def test_an_unconfigured_provider_stops_the_pass_and_leaves_requests_queued(
    writable_tmp_path,
) -> None:
    from marketsentinel.event_analysis import UnavailableArticleAnalysisProvider

    h, sink = _worker(writable_tmp_path, provider=UnavailableArticleAnalysisProvider())
    item = article(1, T0 - timedelta(hours=1))
    h.repository.upsert_articles([item])
    sink.put(article_request_key("ACME", item.fingerprint), {})

    report = _admit(h, sink)

    assert report.stop_reason == "provider_unavailable"
    assert report.articles == ()
    assert report.consumed_keys == ()
    assert h.job(item).state == "pending"


def test_a_failed_request_is_recorded_in_the_ledger_and_still_consumed(writable_tmp_path) -> None:
    h, sink = _worker(writable_tmp_path, provider=ScriptedProvider(always=ValueError("bad")))
    item = article(1, T0 - timedelta(hours=1))
    h.repository.upsert_articles([item])
    sink.put(article_request_key("ACME", item.fingerprint), {})

    report = _admit(h, sink)

    assert report.articles[0][1] in ("retry_wait", "failed")
    assert report.consumed_keys == (f"articles/ACME/{item.fingerprint}.json",)
    assert h.job(item).attempts == 1


def test_malformed_and_unreadable_requests_are_consumed_without_side_effects(
    writable_tmp_path,
) -> None:
    h, sink = _worker(writable_tmp_path)
    (writable_tmp_path / "requests" / "coverage").mkdir(parents=True)
    # Distinct names: the test must also hold on a case-insensitive filesystem.
    (writable_tmp_path / "requests" / "coverage" / "acme.json").write_text("{}", encoding="utf-8")
    (writable_tmp_path / "requests" / "coverage" / "OTHER.json").write_text(
        "not json", encoding="utf-8"
    )

    report = _admit(h, sink)

    assert sorted(report.consumed_keys) == ["coverage/OTHER.json", "coverage/acme.json"]
    assert report.activated == ()
    assert h.repository.list_company_coverage() == []


def test_cycle_order_puts_never_cycled_tickers_first_then_least_recently_attempted() -> None:
    entries = [
        TickerCycleOrder("PFE", T0 - timedelta(hours=1)),
        TickerCycleOrder("AAPL", None),
        TickerCycleOrder("NVDA", T0 - timedelta(hours=7)),
        TickerCycleOrder("AAL", None),
    ]
    assert order_tickers_for_cycle(entries) == ["AAL", "AAPL", "NVDA", "PFE"]


def test_list_company_coverage_reads_every_row(writable_tmp_path) -> None:
    repository = SQLiteRepository(writable_tmp_path / "c.db")
    repository.initialize()
    repository.activate_company_coverage("PFE", started_at=T0, live_window_days=30)
    repository.activate_company_coverage("NVDA", started_at=T0, live_window_days=30)
    assert [row.ticker for row in repository.list_company_coverage()] == ["NVDA", "PFE"]


# --------------------------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--mode", "requests"],
        ["--mode", "cycle"],
        ["--all-active", "--mode", "activate"],
        ["--ticker", "NVDA", "--max-new-total", "-1"],
        ["--ticker", "NVDA", "--max-tickers", "-1"],
    ],
)
def test_cli_rejects_incomplete_or_negative_arguments(argv: list[str]) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        validate_arguments(parser, parser.parse_args(argv))


def test_cli_accepts_the_worker_shapes() -> None:
    parser = build_parser()
    cycle = parser.parse_args(
        ["--all-active", "--max-new", "25", "--max-new-total", "40", "--max-tickers", "12"]
    )
    validate_arguments(parser, cycle)
    assert (cycle.all_active, cycle.max_new_total, cycle.max_tickers) == (True, 40, 12)
    requests = parser.parse_args(
        ["--mode", "requests", "--requests-dir", "r", "--consumed-keys", "c.txt"]
    )
    validate_arguments(parser, requests)
    assert (requests.max_new_tickers, requests.max_article_requests) == (3, 20)
    validate_arguments(parser, parser.parse_args(["--mode", "list-active"]))


def test_cli_requests_and_list_active_modes_run_offline(writable_tmp_path, monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        database_path=writable_tmp_path / "cli.db",
        constituent_cache_path=writable_tmp_path / "missing-cache.json",
    )
    monkeypatch.setattr("scripts.run_coverage_cycle.get_settings", lambda: settings)
    consumed = writable_tmp_path / "out" / "consumed.txt"

    assert main(["--mode", "list-active"]) == 0
    assert (
        main(
            [
                "--mode",
                "requests",
                "--requests-dir",
                str(writable_tmp_path / "empty"),
                "--consumed-keys",
                str(consumed),
            ]
        )
        == 0
    )
    assert consumed.read_text(encoding="utf-8") == ""
