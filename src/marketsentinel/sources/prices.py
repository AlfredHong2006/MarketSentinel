"""Historical market-price provider backed by yfinance."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

import pandas as pd

from marketsentinel.domain import Constituent, PriceHistory, PricePoint
from marketsentinel.errors import ProviderError
from marketsentinel.timeutils import utc_now

LOGGER = logging.getLogger(__name__)


class PriceProvider(Protocol):
    def fetch(self, constituent: Constituent) -> PriceHistory: ...


class CachingPriceProvider:
    """Serve a recently fetched ``PriceHistory`` again instead of re-calling the provider.

    Price history is the one part of the read path that is not persisted, so every page load
    otherwise reaches a third party -- acceptable for a local run, not for a public URL where
    each visitor triggers a fetch.

    Deliberately the smallest thing that works: a per-process, in-memory, per-symbol TTL. No
    database column, no migration, no external cache, and no background refresh. A restart simply
    starts cold.

    Only successful fetches are cached. A ``ProviderError`` is never stored, so a transient
    outage cannot be pinned in place for the whole TTL; the next request retries honestly.
    """

    def __init__(
        self,
        inner: PriceProvider,
        ttl_seconds: float,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._inner = inner
        self._ttl = timedelta(seconds=max(0.0, ttl_seconds))
        self._now = now
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[datetime, PriceHistory]] = {}

    def fetch(self, constituent: Constituent) -> PriceHistory:
        if self._ttl <= timedelta(0):
            return self._inner.fetch(constituent)

        key = constituent.yahoo_symbol
        moment = self._now()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and moment - entry[0] < self._ttl:
                return entry[1]

        # Fetched outside the lock: a slow third-party call must not block reads of other
        # symbols. A rare duplicate fetch on a cold race is cheaper than serialising every read.
        history = self._inner.fetch(constituent)
        with self._lock:
            self._entries[key] = (moment, history)
        return history


class YFinancePriceProvider:
    """Fetch adjusted daily bars for forecasting and the dashboard's one-year display window."""

    def __init__(self, training_period: str = "3y") -> None:
        self.training_period = training_period

    def fetch(self, constituent: Constituent) -> PriceHistory:
        try:
            import yfinance as yf

            frame = yf.Ticker(constituent.yahoo_symbol).history(
                period=self.training_period,
                interval="1d",
                auto_adjust=True,
                actions=False,
                timeout=15,
            )
        except Exception as exc:
            LOGGER.exception("Price request failed for %s", constituent.yahoo_symbol)
            raise ProviderError(f"Price history request failed for {constituent.symbol}") from exc
        if frame.empty or not {"Close", "Volume"}.issubset(frame.columns):
            raise ProviderError(
                f"Price provider returned no valid history for {constituent.symbol}"
            )

        points: list[PricePoint] = []
        for index, row in frame.dropna(subset=["Close"]).iterrows():
            timestamp = pd.Timestamp(index)
            points.append(
                PricePoint(
                    date=timestamp.date(),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume", 0.0) or 0.0),
                )
            )
        if len(points) < 150:
            raise ProviderError(
                f"Only {len(points)} price observations were returned; at least 150 are required"
            )
        return PriceHistory(
            symbol=constituent.yahoo_symbol,
            points=points,
            fetched_at=utc_now(),
        )
