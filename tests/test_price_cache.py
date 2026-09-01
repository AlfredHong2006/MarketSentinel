"""The short-TTL price cache: the smallest thing that stops a public page hammering yfinance.

Price history is the one part of the read path that is not persisted, so every page load
otherwise reaches a third party. These pin the bounds that make the wrapper safe to deploy:
it serves only genuinely fresh values, it never caches a failure, and it never invents a series.
"""

from datetime import UTC, datetime, timedelta

import pytest
from conftest import make_constituent, make_price_history

from marketsentinel.domain import Constituent, PriceHistory
from marketsentinel.errors import ProviderError
from marketsentinel.sources.prices import CachingPriceProvider

START = datetime(2026, 9, 1, 12, tzinfo=UTC)


class Clock:
    """An injected clock: the cache must take no clock of its own."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class CountingProvider:
    """Returns a series stamped with the requested symbol, so a mix-up is detectable."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.failure = failure

    def fetch(self, constituent: Constituent) -> PriceHistory:
        self.calls.append(constituent.yahoo_symbol)
        if self.failure is not None:
            raise self.failure
        history = make_price_history(days=200)
        return history.model_copy(update={"symbol": constituent.yahoo_symbol})


def other_company() -> Constituent:
    return Constituent(symbol="OTHER", yahoo_symbol="OTHER", name="Other Inc", market="S&P 500")


def test_a_second_read_inside_the_ttl_does_not_reach_the_provider() -> None:
    inner = CountingProvider()
    clock = Clock()
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=clock)

    first = cache.fetch(make_constituent())
    clock.advance(300)
    second = cache.fetch(make_constituent())

    assert inner.calls == ["ACME"]
    assert first == second


def test_a_read_after_the_ttl_refetches() -> None:
    inner = CountingProvider()
    clock = Clock()
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=clock)

    cache.fetch(make_constituent())
    clock.advance(901)
    cache.fetch(make_constituent())

    assert inner.calls == ["ACME", "ACME"]


def test_the_ttl_boundary_is_exclusive() -> None:
    """Exactly at the TTL the entry is stale, so freshness is never overstated."""

    inner = CountingProvider()
    clock = Clock()
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=clock)

    cache.fetch(make_constituent())
    clock.advance(900)
    cache.fetch(make_constituent())

    assert inner.calls == ["ACME", "ACME"]


def test_companies_are_cached_independently() -> None:
    """One company's entry must never be served for another."""

    inner = CountingProvider()
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=Clock())

    acme = cache.fetch(make_constituent())
    other = cache.fetch(other_company())

    assert inner.calls == ["ACME", "OTHER"]
    assert acme.symbol == "ACME"
    assert other.symbol == "OTHER"
    # Caching the second company must not have evicted or overwritten the first.
    assert cache.fetch(make_constituent()).symbol == "ACME"
    assert inner.calls == ["ACME", "OTHER"]


def test_a_failure_is_never_cached() -> None:
    """A transient outage must not be pinned in place for the whole TTL."""

    inner = CountingProvider(failure=ProviderError("Price history request failed"))
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=Clock())

    for _ in range(3):
        with pytest.raises(ProviderError):
            cache.fetch(make_constituent())

    assert inner.calls == ["ACME", "ACME", "ACME"]


def test_a_failure_after_a_success_does_not_serve_the_stale_success() -> None:
    """Once expired, an error is reported as an error rather than masked by the old series."""

    inner = CountingProvider()
    clock = Clock()
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=clock)
    cache.fetch(make_constituent())

    clock.advance(1000)
    inner.failure = ProviderError("Price history request failed")
    with pytest.raises(ProviderError):
        cache.fetch(make_constituent())


def test_a_zero_ttl_disables_caching_entirely() -> None:
    """The kill switch: every read reaches the provider, as before the wrapper existed."""

    inner = CountingProvider()
    cache = CachingPriceProvider(inner, ttl_seconds=0, now=Clock())

    cache.fetch(make_constituent())
    cache.fetch(make_constituent())

    assert inner.calls == ["ACME", "ACME"]


def test_the_cache_returns_the_provider_series_unaltered() -> None:
    """A cache may repeat a series; it may never adjust, extend, or refresh one."""

    inner = CountingProvider()
    clock = Clock()
    cache = CachingPriceProvider(inner, ttl_seconds=900, now=clock)

    fresh = inner.fetch(make_constituent())
    cached = cache.fetch(make_constituent())
    clock.advance(300)
    again = cache.fetch(make_constituent())

    assert [p.close for p in cached.points] == [p.close for p in fresh.points]
    assert again.points == cached.points
    # fetched_at is the provider's own stamp, so a repeated read never claims to be newer.
    assert again.fetched_at == cached.fetched_at
