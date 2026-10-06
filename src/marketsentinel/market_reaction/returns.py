"""Adjusted stock/benchmark return alignment on exchange session dates. Nothing is imputed."""

import math
from collections.abc import Iterable
from dataclasses import dataclass

from marketsentinel.market_reaction.calendars import SessionCalendar
from marketsentinel.market_reaction.models import (
    EXTREME_BENCHMARK_SESSION_MOVE,
    EXTREME_STOCK_SESSION_MOVE,
    MAX_PATH_HORIZON,
    PriceObservation,
)


@dataclass(frozen=True)
class SessionPrices:
    """Adjusted closes keyed by calendar session index."""

    closes: dict[int, float]
    invalid_observations: int
    off_calendar_dates: int

    @property
    def first_index(self) -> int | None:
        return min(self.closes) if self.closes else None

    @property
    def last_index(self) -> int | None:
        return max(self.closes) if self.closes else None

    def gap_sessions(self) -> int:
        """Calendar sessions inside the series' own span that carry no price."""

        if not self.closes:
            return 0
        return (self.last_index - self.first_index + 1) - len(self.closes)

    def gap_share(self) -> float:
        if not self.closes:
            return 0.0
        return self.gap_sessions() / (self.last_index - self.first_index + 1)


def align_prices(prices: Iterable[PriceObservation], calendar: SessionCalendar) -> SessionPrices:
    """Keep finite positive closes that fall on a listing-exchange session.

    A price dated off the listing calendar (a benchmark trading on a day the stock's exchange is
    shut) has no compatible session and is dropped and counted, never shifted onto a neighbour.
    """

    closes: dict[int, float] = {}
    invalid = off_calendar = 0
    for observation in prices:
        value = observation.adjusted_close
        if not math.isfinite(value) or value <= 0:
            invalid += 1
            continue
        position = calendar.index_of(observation.date)
        if position is None:
            off_calendar += 1
            continue
        closes[position] = value
    return SessionPrices(
        closes=closes, invalid_observations=invalid, off_calendar_dates=off_calendar
    )


def simple_return(prices: SessionPrices, base: int, target: int) -> float | None:
    start = prices.closes.get(base)
    end = prices.closes.get(target)
    if start is None or end is None:
        return None
    return end / start - 1


@dataclass(frozen=True)
class ReactionPath:
    stock: tuple[float | None, ...]
    benchmark: tuple[float | None, ...]
    market_adjusted: tuple[float | None, ...]
    integrity_flags: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return all(value is not None for value in self.market_adjusted)


def reaction_path(
    stock: SessionPrices,
    benchmark: SessionPrices,
    event_index: int,
    max_horizon: int = MAX_PATH_HORIZON,
) -> ReactionPath:
    """Returns for horizons 0..max_horizon around the event session `t`.

    Horizon 0 is the news session's own move, `P[t] / P[t-1] - 1` (contemporaneous context).
    Horizon h >= 1 is the forward return `P[t+h] / P[t] - 1`. The market-adjusted value is the
    stock return minus the benchmark return over the identical pair of sessions.
    """

    stock_returns: list[float | None] = []
    benchmark_returns: list[float | None] = []
    adjusted: list[float | None] = []
    for horizon in range(max_horizon + 1):
        base, target = (
            (event_index - 1, event_index) if horizon == 0 else (event_index, event_index + horizon)
        )
        stock_value = simple_return(stock, base, target) if base >= 0 else None
        benchmark_value = simple_return(benchmark, base, target) if base >= 0 else None
        stock_returns.append(stock_value)
        benchmark_returns.append(benchmark_value)
        adjusted.append(
            None
            if stock_value is None or benchmark_value is None
            else stock_value - benchmark_value
        )
    return ReactionPath(
        stock=tuple(stock_returns),
        benchmark=tuple(benchmark_returns),
        market_adjusted=tuple(adjusted),
        integrity_flags=_integrity_flags(stock, benchmark, event_index, max_horizon),
    )


def _integrity_flags(
    stock: SessionPrices, benchmark: SessionPrices, event_index: int, max_horizon: int
) -> tuple[str, ...]:
    """Flag large single-session moves inside the window for review. Flags never exclude."""

    flags: list[str] = []
    for label, prices, limit in (
        ("stock", stock, EXTREME_STOCK_SESSION_MOVE),
        ("benchmark", benchmark, EXTREME_BENCHMARK_SESSION_MOVE),
    ):
        for offset in range(max_horizon + 1):
            position = event_index + offset
            move = simple_return(prices, position - 1, position) if position >= 1 else None
            if move is not None and abs(move) >= limit:
                flags.append(f"extreme_{label}_session_move:+{offset}")
    return tuple(flags)
