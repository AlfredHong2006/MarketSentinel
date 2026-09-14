"""Shared public requests: asking never spends, and the worker admits under hard caps.

Offline and deterministic: the request store is a directory (or a fake S3 client), the LLM
provider is scripted, and no network is reached. The two guarantees under test are that a public
request can only ever *ask* -- it writes one small idempotent object and nothing else -- and that
the worker's admission pass consumes exactly what it processed, under caps, so a stranger cannot
create unbounded work or spend.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_article
from fastapi.testclient import TestClient
from test_coverage_ledger import T0, ScriptedProvider, ScriptedSource, article, build
from test_overview import (
    COMPATIBILITY,
    ExplodingNews,
    ExplodingRunner,
    ExplodingSentiment,
    FakePrices,
    read_only_service,
    seed_repository,
    stored_row_counts,
)
from test_public_mode import ExplodingArticleEvents, UniverseConstituents

from marketsentinel.analysis_ledger import LedgeredArticleAnalysisRunner
from marketsentinel.api.app import Services, build_request_sink, build_services, create_app
from marketsentinel.config import Settings
from marketsentinel.domain import CapabilitiesView
from marketsentinel.errors import ArticleAnalysisProviderError, ConstituentNotFoundError
from marketsentinel.event_analysis import (
    OpenAIArticleIntelligenceProvider,
    UnavailableArticleAnalysisProvider,
)
from marketsentinel.forecasting.baseline import BaselineForecaster
from marketsentinel.public_requests import (
    CIRCUIT_BREAKER_CONSECUTIVE_FAILURES,
    R2_CONNECT_TIMEOUT_SECONDS,
    R2_MAX_ATTEMPTS,
    R2_READ_TIMEOUT_SECONDS,
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
from marketsentinel.sentiment.finbert import StaticSentimentAnalyzer
from marketsentinel.service import MarketAnalysisService
from marketsentinel.storage.sqlite import SQLiteRepository
from scripts.publish_public_snapshot import publish
from scripts.run_coverage_cycle import (
    active_tickers_in_cycle_order,
    build_parser,
    main,
    validate_arguments,
)

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


ACCESS_KEY_ID = "R2KEYID0123456789abcdef"
SECRET_ACCESS_KEY = "r2secretvalue0123456789abcdefghijklmnop"


class RaisingS3:
    """A client whose every call raises ``error``, standing in for a real provider failure."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def put_object(self, **kwargs):
        raise self.error

    def get_paginator(self, name):
        raise self.error


def _failing_sink(error: Exception, *, access_key_id: str = ACCESS_KEY_ID) -> R2RequestSink:
    return R2RequestSink(
        bucket="marketsentinel-requests",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id=access_key_id,
        secret_access_key=SECRET_ACCESS_KEY,
        client=RaisingS3(error),
    )


def _logged(caplog) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def test_a_provider_error_is_logged_with_safe_details_and_no_credentials(caplog) -> None:
    from botocore.exceptions import ClientError

    # A real S3/R2 signature failure body carries the key id and the string to sign; neither may
    # reach the log, while the code, message, status and request id must.
    error = ClientError(
        {
            "Error": {
                "Code": "SignatureDoesNotMatch",
                "Message": "The request signature we calculated does not match",
                "AWSAccessKeyId": ACCESS_KEY_ID,
                "StringToSign": "AWS4-HMAC-SHA256\n20260913T120000Z\nsecret-derived",
                "CanonicalRequest": "PUT\n/marketsentinel-requests/coverage/AMZN.json",
            },
            "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "req-123"},
        },
        "PutObject",
    )
    sink = _failing_sink(error)

    with (
        caplog.at_level("ERROR", logger="marketsentinel.public_requests"),
        pytest.raises(RequestSinkError, match="could not record the request"),
    ):
        sink.put(coverage_request_key("AMZN"), {"kind": "coverage"})

    text = _logged(caplog)
    assert caplog.records[0].levelname == "ERROR"
    assert "R2 put_object failed" in text
    assert "type=botocore.exceptions.ClientError" in text
    assert "code='SignatureDoesNotMatch'" in text
    assert "http_status=403" in text
    assert "request_id='req-123'" in text
    assert "bucket='marketsentinel-requests'" in text
    assert "key='coverage/AMZN.json'" in text
    assert "endpoint='https://acct.r2.cloudflarestorage.com'" in text
    assert "Traceback" in text
    for forbidden in (ACCESS_KEY_ID, SECRET_ACCESS_KEY, "StringToSign", "secret-derived"):
        assert forbidden not in text


def test_an_http_client_error_quoting_the_authorization_header_is_redacted(caplog) -> None:
    """A key id with a trailing newline makes the HTTP client quote the SigV4 header."""

    from botocore.exceptions import HTTPClientError

    header = (
        # The repr form the HTTP client actually quotes: an escaped "\n" after the key id.
        f"AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}\\n/20260913/auto/s3/aws4_request, "
        "SignedHeaders=content-type;host, Signature=0123456789abcdef0123456789abcdef"
    )
    error = HTTPClientError(error=ValueError(f"Invalid header value b'{header}'"))
    sink = _failing_sink(error, access_key_id=f"{ACCESS_KEY_ID}\n")

    with (
        caplog.at_level("ERROR", logger="marketsentinel.public_requests"),
        pytest.raises(RequestSinkError),
    ):
        sink.put(coverage_request_key("AMZN"), {"kind": "coverage"})

    text = _logged(caplog)
    assert "type=botocore.exceptions.HTTPClientError" in text
    assert "Invalid header value" in text
    assert "Credential=[redacted]" in text
    assert "Signature=[redacted]" in text
    assert "access_key_id=True" in text
    assert "secret=False" in text
    for forbidden in (ACCESS_KEY_ID, SECRET_ACCESS_KEY, "0123456789abcdef0123456789abcdef"):
        assert forbidden not in text


def test_a_listing_failure_is_logged_without_a_key(caplog) -> None:
    from botocore.exceptions import EndpointConnectionError

    sink = _failing_sink(
        EndpointConnectionError(endpoint_url="https://acct.r2.cloudflarestorage.com")
    )

    with (
        caplog.at_level("ERROR", logger="marketsentinel.public_requests"),
        pytest.raises(RequestSinkError, match="could not list"),
    ):
        sink.list_keys()

    text = _logged(caplog)
    assert "R2 list_objects_v2 failed" in text
    assert "type=botocore.exceptions.EndpointConnectionError" in text
    assert "key=None" in text
    assert ACCESS_KEY_ID not in text and SECRET_ACCESS_KEY not in text


def test_the_public_response_stays_generic_when_the_store_fails(writable_tmp_path, caplog) -> None:
    from botocore.exceptions import ClientError

    repository = seed_repository(writable_tmp_path)
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject"
    )
    with (
        caplog.at_level("ERROR", logger="marketsentinel.public_requests"),
        TestClient(_app(repository, _service(repository, _failing_sink(error)))) as client,
    ):
        response = client.post("/api/v1/companies/HIDDEN1/coverage-requests")

    assert response.status_code == 503
    assert response.json() == {"detail": "The request store is unavailable. Try again later."}
    assert "code='AccessDenied'" in _logged(caplog)


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


# --------------------------------------------------------------------------------------------
# Launch hardening: configuration, credentials, cost control, and the full round trip
# --------------------------------------------------------------------------------------------


def test_credential_bucket_and_url_settings_are_stripped_of_surrounding_whitespace() -> None:
    """A request-store credential pasted with a trailing newline broke every public request in
    production (the HTTP client refuses the signed header). Such a value can never be the
    intended one, so it is stripped on load; whitespace-only reads as unset."""

    settings = Settings(
        _env_file=None,
        public_requests_bucket=" marketsentinel-requests\n",
        public_requests_endpoint_url="https://acct.r2.cloudflarestorage.com\n",
        public_requests_access_key_id=f" {ACCESS_KEY_ID}\n",
        public_requests_secret_access_key=f"{SECRET_ACCESS_KEY} \r\n",
        public_snapshot_manifest_url="\thttps://pub.example/latest.json ",
        llm_api_key="   ",
        llm_base_url=" https://api.openai.com/v1 ",
        hf_token=" hf_token ",
    )

    assert settings.public_requests_bucket == "marketsentinel-requests"
    assert settings.public_requests_endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert settings.public_requests_access_key_id == ACCESS_KEY_ID
    assert settings.public_requests_secret_access_key == SECRET_ACCESS_KEY
    assert settings.public_snapshot_manifest_url == "https://pub.example/latest.json"
    assert settings.llm_api_key is None
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.hf_token == "hf_token"
    # Untouched defaults stay exactly as shipped.
    assert Settings(_env_file=None).llm_base_url == "https://api.openai.com/v1"
    assert Settings(_env_file=None).public_requests_bucket is None

    sink = build_request_sink(settings)
    assert isinstance(sink, R2RequestSink)
    assert sink.bucket == "marketsentinel-requests"
    assert sink._endpoint_url == "https://acct.r2.cloudflarestorage.com"  # noqa: SLF001
    assert sink._access_key_id == ACCESS_KEY_ID  # noqa: SLF001
    assert sink._secret_access_key == SECRET_ACCESS_KEY  # noqa: SLF001


def test_public_mode_never_wires_a_configured_provider_even_when_a_key_is_present(
    writable_tmp_path,
) -> None:
    """The public host must never hold an LLM credential. If one is set there by mistake, no
    code path may be able to spend it: public mode builds the unavailable provider and no
    automatic refresh runner, independently of the endpoint-level refusal."""

    common = {
        "_env_file": None,
        "llm_api_key": "sk-should-never-be-used",
        "database_path": writable_tmp_path / "wired.db",
        "constituent_cache_path": writable_tmp_path / "missing-cache.json",
    }

    public = build_services(Settings(public_mode=True, **common))
    assert public.analysis.article_analysis_runner is None
    assert isinstance(
        public.article_events.analysis_service.provider, UnavailableArticleAnalysisProvider
    )

    # Private interactive behaviour is unchanged: the key is honoured as before.
    private = build_services(Settings(public_mode=False, **common))
    assert isinstance(private.analysis.article_analysis_runner, LedgeredArticleAnalysisRunner)
    assert isinstance(
        private.article_events.analysis_service.provider, OpenAIArticleIntelligenceProvider
    )


def test_the_r2_client_is_built_with_bounded_timeouts_and_retries() -> None:
    """An unreachable store must fail a request, or the startup mirror rebuild, in seconds
    rather than holding a thread for boto3's multi-minute defaults. Builds a real (offline)
    client: nothing connects until a call is made."""

    sink = R2RequestSink(
        bucket="requests",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        access_key_id="k",
        secret_access_key="s",
    )

    client = sink._s3()  # noqa: SLF001

    assert client.meta.config.connect_timeout == R2_CONNECT_TIMEOUT_SECONDS
    assert client.meta.config.read_timeout == R2_READ_TIMEOUT_SECONDS
    # botocore normalises max_attempts to the total including the first try.
    assert client.meta.config.retries == {
        "mode": "standard",
        "total_max_attempts": R2_MAX_ATTEMPTS + 1,
    }
    assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
    assert sink._s3() is client  # noqa: SLF001 -- built once, reused


class StrictS3:
    """Fails on any operation other than the two the sink is allowed: put and list."""

    def __init__(self) -> None:
        self.buckets: list[str] = []

    def put_object(self, **kwargs):
        self.buckets.append(kwargs["Bucket"])

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        buckets = self.buckets

        class Paginator:
            def paginate(self, *, Bucket, Prefix):
                del Prefix
                buckets.append(Bucket)
                yield {}

        return Paginator()

    def __getattr__(self, name):
        raise AssertionError(f"the request sink called {name!r}, which it must never do")


def test_the_request_sink_only_puts_and_lists_and_only_in_its_own_bucket() -> None:
    """The public credential is scoped to the request bucket; the sink itself must also never
    read, delete, or address any other bucket, so a wider credential would still not be used."""

    client = StrictS3()
    sink = R2RequestSink(
        bucket="requests-only", endpoint_url="https://r2.test", access_key_id="k",
        secret_access_key="s", client=client,
    )  # fmt: skip

    sink.put(coverage_request_key("AMZN"), {"kind": "coverage"})
    sink.put(article_request_key("AMZN", "f1"), {"kind": "article"})
    sink.list_keys()

    assert set(client.buckets) == {"requests-only"}


class GrammarBreakingConstituents(TwoCompanyConstituents):
    """Resolves a symbol that does not fit the request-key grammar."""

    def resolve_cached(self, symbol: str):
        if symbol == "BAD":
            return self.resolve("ACME").model_copy(update={"symbol": "BAD/../SYM"})
        return self.resolve(symbol)


def test_a_symbol_outside_the_key_grammar_is_refused_and_nothing_is_recorded(
    writable_tmp_path,
) -> None:
    repository = seed_repository(writable_tmp_path)
    sink = DirectoryRequestSink(writable_tmp_path / "requests")
    service = _service(repository, sink)
    service.constituents = GrammarBreakingConstituents()
    unanalysed = make_article(title="An unanalysed Acme story", url="https://example.com/u1")
    repository.upsert_articles([unanalysed])

    with TestClient(_app(repository, service)) as client:
        coverage = client.post("/api/v1/companies/BAD/coverage-requests")
        analysis = client.post(
            f"/api/v1/companies/BAD/articles/{unanalysed.fingerprint}/analysis-requests"
        )
        odd_id = client.post("/api/v1/companies/ACME/articles/has%20space/analysis-requests")

    assert coverage.status_code == 409
    assert analysis.status_code == 409
    assert odd_id.status_code == 404
    assert sink.list_keys() == []
    assert not (writable_tmp_path / "requests").exists()


def test_consecutive_paid_failures_trip_the_admission_breaker_and_leave_the_rest_queued(
    writable_tmp_path,
) -> None:
    """A provider failing every call (expired key, exhausted quota) must not burn the whole
    per-run cap. Like the cycle's own breaker: two consecutive paid failures stop the pass; the
    attempted keys are consumed (the ledger holds their retry state), the rest stay queued."""

    failing = ScriptedProvider(always=ArticleAnalysisProviderError("http_error"))
    h, sink = _worker(writable_tmp_path, provider=failing)
    items = [article(index, T0 - timedelta(hours=index)) for index in range(1, 6)]
    h.repository.upsert_articles(items)
    for index, item in enumerate(items):
        sink.put(
            article_request_key("ACME", item.fingerprint),
            {"requested_at": f"2026-09-01T0{index}:00:00+00:00"},
        )

    report = _admit(h, sink, max_article_requests=20)

    assert h.provider.stage_a_calls == CIRCUIT_BREAKER_CONSECUTIVE_FAILURES
    assert report.stop_reason == "circuit_breaker"
    assert [status for _, status in report.articles] == ["retry_wait", "retry_wait"]
    assert report.deferred_articles == tuple(item.fingerprint for item in items[2:])
    assert sorted(report.consumed_keys) == sorted(
        article_request_key("ACME", item.fingerprint) for item in items[:2]
    )
    assert "stopped: circuit_breaker" in report.render()
    # Every key is still in the store: only the workflow deletes, and only what was consumed.
    assert len(sink.list_keys()) == 5

    # A success in between resets the count, so an isolated failure never trips it.
    h.provider.always = None
    h.provider.failures = [ArticleAnalysisProviderError("http_error")]
    for item in items[:2]:
        (writable_tmp_path / "requests" / article_request_key("ACME", item.fingerprint)).unlink()
    recovered = _admit(h, sink, max_article_requests=20)
    assert recovered.stop_reason is None
    assert [status for _, status in recovered.articles] == ["retry_wait", "analyzed", "analyzed"]


def test_all_active_cycle_order_rotates_so_a_capped_run_is_round_robin(writable_tmp_path) -> None:
    h = build(writable_tmp_path, sources=[ScriptedSource(name="wire")])
    h.cycle.constituents = TwoCompanyConstituents()
    for ticker in ("OTHER", "ACME"):
        h.cycle.activate(ticker, now=T0)

    # Never cycled: alphabetical.
    assert active_tickers_in_cycle_order(h.cycle) == ["ACME", "OTHER"]
    # A run of the first ticker (as --max-tickers 1 would do) moves it behind the other.
    h.cycle.run("ACME", now=T0 + timedelta(minutes=1), max_new_analyses=0)
    assert active_tickers_in_cycle_order(h.cycle) == ["OTHER", "ACME"]
    h.cycle.run("OTHER", now=T0 + timedelta(minutes=2), max_new_analyses=0)
    assert active_tickers_in_cycle_order(h.cycle) == ["ACME", "OTHER"]


class SnapshotResolver(TwoCompanyConstituents):
    def resolve_cached(self, symbol: str):
        return self.resolve(symbol.upper())


def _public_app_over_snapshot(database_path, sink, compatibility):
    """A public deployment over one published snapshot, with a request store attached."""

    repository = SQLiteRepository(database_path)
    repository.initialize()
    analysis = MarketAnalysisService(
        constituents=SnapshotResolver(),
        news=ExplodingNews(),
        historical_news=ExplodingNews(),
        sentiment=ExplodingSentiment(),
        prices=FakePrices(),
        repository=repository,
        forecaster=BaselineForecaster(),
        article_analysis_compatibility=compatibility,
        article_analysis_runner=ExplodingRunner(),
    )
    requests = PublicRequestService(
        sink=sink,
        repository=repository,
        constituents=SnapshotResolver(),
        compatibility=compatibility,
        limits=LIMITS,
        clock=Clock(datetime(2026, 9, 13, 12, 0, tzinfo=UTC)),
    )
    return create_app(
        settings=PUBLIC,
        services=Services(
            repository=repository,
            constituents=SnapshotResolver(),
            analysis=analysis,
            article_events=ExplodingArticleEvents(),
            requests=requests,
        ),
    )


def _publish(writable_tmp_path, name: str, tickers: list[str]):
    cache = writable_tmp_path / "constituents_cache.json"
    cache.write_text(json.dumps({"source": "test", "constituents": []}), encoding="utf-8")
    output = writable_tmp_path / name
    assert (
        publish(
            database=writable_tmp_path / "ledger.db",
            constituent_cache=cache,
            output_dir=output,
            public_base_url="https://pub.test",
            tickers=tickers,
            now=T0,
        )
        == 0
    )
    version = (output / "version.txt").read_text(encoding="utf-8")
    return output / version / "public-snapshot.db"


def test_the_full_round_trip_from_public_request_to_published_analysis(writable_tmp_path) -> None:
    """Public request -> store -> worker admission -> (crash and re-admission pays nothing) ->
    consumed keys deleted -> snapshot published -> the public deployment serves the result and
    reports the request as covered / analysed."""

    h, sink = _worker(writable_tmp_path)
    items = [article(index, T0 - timedelta(hours=index)) for index in range(1, 3)]
    h.repository.upsert_articles(items)
    h.repository.upsert_sentiments(StaticSentimentAnalyzer().score(items))
    assert h.service.analyze_article(items[0].fingerprint).status == "generated"
    paid_before = h.provider.stage_a_calls
    compatibility = h.runner.compatibility

    # Snapshot 1: one analysed article, nothing covered.
    first = _publish(writable_tmp_path, "dist-1", ["ACME"])
    with TestClient(_public_app_over_snapshot(first, sink, compatibility)) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["supports_coverage_requests"] is True
        assert capabilities["covered_companies"] == []
        rows = client.get("/api/v1/companies/ACME/articles").json()["articles"]
        assert [row["has_compatible_analysis"] for row in rows] == [True, False]

        base = "/api/v1/companies/ACME/articles"
        analysed = client.post(f"{base}/{items[0].fingerprint}/analysis-requests")
        queued = client.post(f"{base}/{items[1].fingerprint}/analysis-requests")
        again = client.post(f"{base}/{items[1].fingerprint}/analysis-requests")
        coverage = client.post("/api/v1/companies/other/coverage-requests")
        coverage_again = client.post("/api/v1/companies/OTHER/coverage-requests")
        unknown = client.post("/api/v1/companies/NOPE/coverage-requests")
        capabilities = client.get("/api/v1/capabilities").json()

    assert analysed.json()["state"] == "analysed"
    assert (queued.status_code, queued.json()["state"]) == (202, "queued")
    assert again.json()["state"] == "already_queued"
    assert (coverage.json()["symbol"], coverage.json()["state"]) == ("OTHER", "queued")
    assert coverage_again.json()["state"] == "already_queued"
    assert unknown.status_code == 404
    assert capabilities["pending_coverage_requests"] == ["OTHER"]
    assert capabilities["pending_article_requests"] == [items[1].fingerprint]
    assert sorted(sink.list_keys()) == [
        f"articles/ACME/{items[1].fingerprint}.json",
        "coverage/OTHER.json",
    ]

    # Worker admission: one activation (free), one generated analysis (paid once).
    report = _admit(h, sink)
    assert report.activated == ("OTHER",)
    assert report.articles == ((items[1].fingerprint, "analyzed"),)
    assert h.provider.stage_a_calls == paid_before + 1
    assert sorted(report.consumed_keys) == sorted(sink.list_keys())

    # The run failed before its checkpoint, so nothing was deleted: re-admission is free.
    repeat = _admit(h, sink)
    assert repeat.already_covered == ("OTHER",)
    assert repeat.articles == ((items[1].fingerprint, "analyzed"),)
    assert h.provider.stage_a_calls == paid_before + 1

    # After the checkpoint the workflow deletes exactly the consumed keys.
    for key in report.consumed_keys:
        (writable_tmp_path / "requests" / key).unlink()
    assert sink.list_keys() == []

    # Snapshot 2: the public deployment now serves the analysis and the coverage state.
    second = _publish(writable_tmp_path, "dist-2", ["ACME", "OTHER"])
    with TestClient(_public_app_over_snapshot(second, sink, compatibility)) as client:
        capabilities = client.get("/api/v1/capabilities").json()
        rows = client.get("/api/v1/companies/ACME/articles").json()["articles"]
        detail = client.get(f"/api/v1/companies/ACME/articles/{items[1].fingerprint}/analysis")
        settled = client.post(
            f"/api/v1/companies/ACME/articles/{items[1].fingerprint}/analysis-requests"
        )
        covered = client.post("/api/v1/companies/OTHER/coverage-requests")

    assert capabilities["covered_companies"] == ["OTHER"]
    assert capabilities["pending_coverage_requests"] == []
    assert capabilities["pending_article_requests"] == []
    assert [row["has_compatible_analysis"] for row in rows] == [True, True]
    assert detail.status_code == 200
    assert detail.json()["article_id"] == items[1].fingerprint
    assert settled.json()["state"] == "analysed"
    assert covered.json()["state"] == "covered"
    assert sink.list_keys() == []
