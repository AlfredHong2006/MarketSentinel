"""Public deployment mode: broadly searchable, strictly read-only.

Public mode's only enforcement is closing the two endpoints that spend money or mutate stored
data. Search and every read endpoint serve the same full constituent universe in both modes --
the configured prepared set is a label for deliberately backfilled deep coverage, never a gate,
and a company with nothing stored is served as an honest empty overview rather than hidden.

Everything here is asserted at the API boundary, because a client that hides a button restricts
nothing: the API is reachable on its own.
"""

from datetime import UTC, datetime

import pytest
from conftest import make_article, make_constituent
from fastapi.testclient import TestClient
from test_overview import (
    CachedConstituents,
    FakePrices,
    read_only_service,
    seed_repository,
    stored_row_counts,
)

from marketsentinel.api.app import Services, create_app
from marketsentinel.config import Settings
from marketsentinel.domain import CapabilitiesView, Constituent, UniverseResult
from marketsentinel.errors import ConstituentNotFoundError
from marketsentinel.service import NO_STORED_COVERAGE_PRICE_MESSAGE

PUBLIC = Settings(
    public_mode=True, public_prepared_companies=("ACME", "OTHER"), public_default_symbol="ACME"
)
PRIVATE = Settings(public_mode=False, public_prepared_companies=("ACME", "OTHER"))

HIDDEN_COUNT = 30


def _hidden(index: int) -> Constituent:
    return Constituent(
        symbol=f"HIDDEN{index}",
        yahoo_symbol=f"HIDDEN{index}",
        name=f"Hidden Co {index}",
        market="S&P 500",
    )


def _universe() -> list[Constituent]:
    """Uncovered companies rank FIRST, past the default page size, like a real broad universe."""

    return [_hidden(index) for index in range(HIDDEN_COUNT)] + [
        make_constituent(),
        Constituent(symbol="OTHER", yahoo_symbol="OTHER", name="Other Inc", market="S&P 500"),
    ]


class UniverseConstituents(CachedConstituents):
    """A full-universe fake that honours ``limit`` and resolves every listed company."""

    def search(self, query: str = "", market: str | None = None, limit: int = 20) -> UniverseResult:
        del query, market
        return UniverseResult(
            constituents=_universe()[:limit],
            source="test",
            is_fallback=False,
            fetched_at=datetime.now(UTC),
        )

    def resolve_cached(self, symbol: str):
        for item in _universe():
            if item.symbol == symbol.upper():
                return item
        raise ConstituentNotFoundError(f"{symbol!r} is not in the available constituent universe")


class ExplodingArticleEvents:
    """Reaching the generating provider at all is the failure this guards."""

    def analyze_article(self, article_id: str):
        raise AssertionError(f"a public request generated an analysis for {article_id!r}")


def build_app(repository, settings: Settings, service=None):
    if service is None:
        service = read_only_service(repository, FakePrices())
    # The read-only service ships with the ACME-only resolver; widen it to the full fake
    # universe so non-prepared companies resolve the way real constituents do.
    service.constituents = UniverseConstituents()
    return create_app(
        settings=settings,
        services=Services(
            repository=repository,
            constituents=UniverseConstituents(),
            analysis=service,
            article_events=ExplodingArticleEvents(),
        ),
    )


# --------------------------------------------------------------------------------------------
# Capabilities: mode, prepared labels, and the raw coverage map
# --------------------------------------------------------------------------------------------


def test_capabilities_reports_public_scope(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PUBLIC)) as client:
        view = CapabilitiesView.model_validate(client.get("/api/v1/capabilities").json())

    assert view.mode == "public"
    assert view.prepared_companies == ["ACME", "OTHER"]
    assert view.default_symbol == "ACME"
    assert view.supports_refresh is False
    assert view.supports_article_analysis is False


def test_capabilities_reports_private_mode_as_unrestricted(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PRIVATE)) as client:
        view = CapabilitiesView.model_validate(client.get("/api/v1/capabilities").json())

    assert view.mode == "private"
    assert view.supports_refresh is True
    assert view.supports_article_analysis is True
    # Prepared companies are a label, not an allowlist, so they are reported in both modes.
    assert view.prepared_companies == ["ACME", "OTHER"]


def test_capabilities_coverage_is_the_raw_stored_article_count(writable_tmp_path) -> None:
    """One count per ticker with stored rows -- a database fact, never a quality score."""

    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PUBLIC)) as client:
        coverage = client.get("/api/v1/capabilities").json()["coverage"]

    # seed_repository stores exactly one genuine ACME article; no other ticker has rows.
    assert coverage == {"ACME": 1}


def test_capabilities_coverage_excludes_demo_rows(writable_tmp_path) -> None:
    """A demo fallback row must never inflate a company's public coverage figure."""

    repository = seed_repository(writable_tmp_path)
    second_real = make_article(title="A second genuine Acme story", url="https://example.com/r2")
    demo = make_article(title="A demo Acme story", url="https://example.com/d1").model_copy(
        update={"is_demo": True}
    )
    repository.upsert_articles([second_real, demo])

    with TestClient(build_app(repository, PUBLIC)) as client:
        coverage = client.get("/api/v1/capabilities").json()["coverage"]

    assert coverage == {"ACME": 2}


# --------------------------------------------------------------------------------------------
# Search: the full universe, identically in both modes
# --------------------------------------------------------------------------------------------


def test_public_search_serves_the_full_constituent_universe(writable_tmp_path) -> None:
    """Companies without stored coverage stay searchable; labelling them is the client's job."""

    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PUBLIC)) as client:
        payload = client.get("/api/v1/constituents/search?q=&market=All").json()

    symbols = [item["symbol"] for item in payload["constituents"]]
    assert "HIDDEN0" in symbols
    assert len(symbols) == 20  # the default page size, untouched by any public filtering


def test_search_is_identical_in_public_and_private_modes(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PUBLIC)) as public_client:
        public_payload = public_client.get("/api/v1/constituents/search?q=&market=All").json()
    with TestClient(build_app(repository, PRIVATE)) as private_client:
        private_payload = private_client.get("/api/v1/constituents/search?q=&market=All").json()

    assert public_payload["constituents"] == private_payload["constituents"]


# --------------------------------------------------------------------------------------------
# Reads: any resolvable company, with an honest zero-coverage state
# --------------------------------------------------------------------------------------------


def test_public_reads_serve_a_company_outside_the_prepared_set(writable_tmp_path) -> None:
    """Prepared is a label. A non-prepared company with stored data serves its real overview."""

    repository = seed_repository(writable_tmp_path)
    public = Settings(
        public_mode=True, public_prepared_companies=("OTHER",), public_default_symbol="OTHER"
    )
    with TestClient(build_app(repository, public)) as client:
        response = client.get("/api/v1/companies/ACME/overview")

    response.raise_for_status()
    assert response.json()["coverage"]["analysed_articles"] == 1


def test_a_zero_coverage_company_is_served_empty_without_a_price_fetch(writable_tmp_path) -> None:
    """The intentional public empty state: real contract, zero stored rows, no external call."""

    repository = seed_repository(writable_tmp_path)
    prices = FakePrices()
    service = read_only_service(repository, prices)
    with TestClient(build_app(repository, PUBLIC, service)) as client:
        response = client.get("/api/v1/companies/HIDDEN5/overview")
        articles = client.get("/api/v1/companies/HIDDEN5/articles")

    response.raise_for_status()
    payload = response.json()
    assert prices.calls == 0
    assert payload["coverage"]["articles"] == 0
    assert payload["key_developments"]["rows"] == []
    assert payload["top_risks"]["rows"] == []
    assert payload["chart"]["status"] == "unavailable"
    assert payload["chart"]["message"] == NO_STORED_COVERAGE_PRICE_MESSAGE
    assert articles.json()["articles"] == []


def test_an_unknown_symbol_is_still_not_found(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PUBLIC)) as client:
        assert client.get("/api/v1/companies/NOPE/overview").status_code == 404


def test_public_reads_still_write_nothing(writable_tmp_path) -> None:
    repository = seed_repository(writable_tmp_path)
    before = stored_row_counts(repository)

    with TestClient(build_app(repository, PUBLIC)) as client:
        assert client.get("/api/v1/companies/ACME/overview").status_code == 200
        assert client.get("/api/v1/companies/HIDDEN5/overview").status_code == 200
        assert client.get("/api/v1/companies/ACME/articles").status_code == 200
        assert client.get("/api/v1/capabilities").status_code == 200

    assert stored_row_counts(repository) == before


def test_public_default_symbol_is_reachable_through_the_public_endpoints(
    writable_tmp_path,
) -> None:
    """A deployment whose default company cannot be read would open on an error page."""

    repository = seed_repository(writable_tmp_path)
    with TestClient(build_app(repository, PUBLIC)) as client:
        default = client.get("/api/v1/capabilities").json()["default_symbol"]
        assert client.get(f"/api/v1/companies/{default}/overview").status_code == 200


# --------------------------------------------------------------------------------------------
# The two spending endpoints: the whole of public mode's enforcement
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/analyze", {"symbol": "ACME"}),
        ("/api/v1/articles/analyze", {"article_id": "any-article"}),
    ],
)
def test_public_mode_refuses_the_endpoints_that_spend(
    writable_tmp_path, path: str, body: dict
) -> None:
    """These cost money and mutate stored data, so a public deployment must not expose them."""

    repository = seed_repository(writable_tmp_path)
    before = stored_row_counts(repository)

    class ExplodingAnalyze:
        def analyze(self, symbol: str):
            raise AssertionError(f"a public request refreshed coverage for {symbol!r}")

    service = read_only_service(repository, FakePrices())
    service.analyze = ExplodingAnalyze().analyze  # type: ignore[method-assign]

    with TestClient(build_app(repository, PUBLIC, service)) as client:
        response = client.post(path, json=body)

    assert response.status_code == 404
    assert stored_row_counts(repository) == before


def test_private_mode_leaves_the_spending_endpoints_reachable(writable_tmp_path) -> None:
    """The public refusal must not be a behaviour change for a local run."""

    repository = seed_repository(writable_tmp_path)

    class RecordingAnalyze:
        called = False

        def analyze(self, symbol: str):
            RecordingAnalyze.called = True
            raise RuntimeError("reached the service, which is the point")

    service = read_only_service(repository, FakePrices())
    service.analyze = RecordingAnalyze().analyze  # type: ignore[method-assign]

    with TestClient(build_app(repository, PRIVATE, service)) as client:
        response = client.post("/api/v1/analyze", json={"symbol": "ACME"})

    # Reached the service and failed there (500), rather than being refused at the boundary (404).
    assert RecordingAnalyze.called is True
    assert response.status_code == 500


def test_shipped_default_settings_keep_a_local_run_private() -> None:
    """The committed defaults are the private ones; public mode is opt-in configuration."""

    settings = Settings()
    assert settings.public_mode is False
    assert settings.public_prepared_companies == ("NVDA", "PFE")
    assert settings.public_default_symbol == "NVDA"
