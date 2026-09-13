"""Offline test for the constituent-cache warm-up script.

Only the no-op branch is exercised without network: when the cache file already exists, this
script must not touch the network at all (the whole point -- it exists so the live fetch only
ever happens once, on a genuinely cold start).
"""

from marketsentinel.config import get_settings
from scripts.warm_constituent_cache import main


def test_no_op_when_cache_already_present(writable_tmp_path, monkeypatch) -> None:
    cache_path = writable_tmp_path / "constituents_cache.json"
    cache_path.write_text('{"source": "existing"}', encoding="utf-8")
    monkeypatch.setenv("MARKETSENTINEL_CONSTITUENT_CACHE_PATH", str(cache_path))
    get_settings.cache_clear()

    try:
        assert main(["NVDA"]) == 0
        assert cache_path.read_text(encoding="utf-8") == '{"source": "existing"}'
    finally:
        get_settings.cache_clear()


def test_missing_tickers_returns_two() -> None:
    assert main([]) == 2
