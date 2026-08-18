"""Historical market-price provider backed by yfinance."""

import logging
from typing import Protocol

import pandas as pd

from marketsentinel.domain import Constituent, PriceHistory, PricePoint
from marketsentinel.errors import ProviderError
from marketsentinel.timeutils import utc_now

LOGGER = logging.getLogger(__name__)


class PriceProvider(Protocol):
    def fetch(self, constituent: Constituent) -> PriceHistory: ...


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
