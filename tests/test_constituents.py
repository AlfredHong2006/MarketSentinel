import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import marketsentinel.constituents as constituent_module
from marketsentinel.constituents import CacheOnlyConstituentResolver, WikipediaConstituentService
from marketsentinel.domain import Constituent, UniverseResult
from marketsentinel.errors import ConstituentNotFoundError


class FakeResponse:
    text = """
        <table id="constituents">
          <tr><th>Symbol</th><th>Security</th></tr>
          <tr><td>BRK.B</td><td>Berkshire Hathaway</td></tr>
        </table>
    """

    def raise_for_status(self) -> None:
        return None


def test_constituent_parser_accepts_wikipedia_table_without_thead(monkeypatch) -> None:
    monkeypatch.setattr(
        constituent_module,
        "_SOURCES",
        {"S&P 500": ("https://example.test", "Symbol", "Security", 1)},
    )
    monkeypatch.setattr(constituent_module.httpx, "get", lambda *args, **kwargs: FakeResponse())
    service = WikipediaConstituentService(Path("unused.json"))

    result = service._fetch_market("S&P 500")

    assert result[0].symbol == "BRK.B"
    assert result[0].yahoo_symbol == "BRK-B"


def _write_cache(path: Path) -> None:
    universe = UniverseResult(
        constituents=[
            Constituent(symbol="NVDA", yahoo_symbol="NVDA", name="Nvidia", market="S&P 500")
        ],
        source="test cache",
        is_fallback=False,
        fetched_at=datetime.now(UTC),
    )
    path.write_text(json.dumps(universe.model_dump(mode="json")), encoding="utf-8")


def _forbid_network(monkeypatch) -> None:
    def explode(*args, **kwargs):
        raise AssertionError("cache-only resolution must never reach the network")

    monkeypatch.setattr(constituent_module.httpx, "get", explode)


def test_cache_only_resolution_uses_a_stale_cache_instead_of_fetching(
    writable_tmp_path, monkeypatch
) -> None:
    cache_path = writable_tmp_path / "constituents.json"
    _write_cache(cache_path)
    _forbid_network(monkeypatch)
    # Older than the refresh interval: ordinary load() would refresh, cache-only must not.
    service = WikipediaConstituentService(cache_path, cache_ttl=timedelta(seconds=0))

    resolved = CacheOnlyConstituentResolver(service).resolve("NVDA")

    assert resolved.symbol == "NVDA"
    assert service.load_cached().constituents


def test_cache_only_resolution_fails_clearly_when_no_cache_exists(
    writable_tmp_path, monkeypatch
) -> None:
    _forbid_network(monkeypatch)
    service = WikipediaConstituentService(writable_tmp_path / "missing.json")

    with pytest.raises(ConstituentNotFoundError, match="cannot fetch"):
        CacheOnlyConstituentResolver(service).resolve("NVDA")


def test_cache_only_load_never_forces_a_refresh(writable_tmp_path, monkeypatch) -> None:
    cache_path = writable_tmp_path / "constituents.json"
    _write_cache(cache_path)
    _forbid_network(monkeypatch)
    resolver = CacheOnlyConstituentResolver(WikipediaConstituentService(cache_path))

    assert resolver.load(force_refresh=True).constituents[0].symbol == "NVDA"


def test_ordinary_resolution_still_refreshes_over_the_network(
    writable_tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        constituent_module,
        "_SOURCES",
        {"S&P 500": ("https://example.test", "Symbol", "Security", 1)},
    )
    monkeypatch.setattr(constituent_module.httpx, "get", lambda *args, **kwargs: FakeResponse())
    service = WikipediaConstituentService(writable_tmp_path / "refreshed-cache.json")

    assert service.resolve("BRK.B").name == "Berkshire Hathaway"
