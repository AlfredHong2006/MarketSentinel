"""Exchange-aware information-time alignment for `mr-v1`.

An article belongs to the first listing-exchange session whose close occurs *after* publication.
Session closes come from `exchange_calendars`, so holidays, early closes, and each exchange's own
daylight-saving rules are data rather than a hardcoded global close time.

`SessionCalendar` is a plain frozen value: once built, assignment takes no clock and no I/O. The
bounds are always passed explicitly because the library's default end date is clock-derived.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from marketsentinel.market_reaction.models import ListingExchange

_EXCHANGE_TIMEZONES = {
    ListingExchange.US: ZoneInfo("America/New_York"),
    ListingExchange.LONDON: ZoneInfo("Europe/London"),
}
# Investable trackers, not bare price indices: SPY and CUKX.L are total-return instruments whose
# adjusted closes fold in dividends the same way the stock's adjusted closes do, so a
# market-adjusted return subtracts like for like. ^GSPC / ^FTSE remain useful as reference series
# where they are already held, but they are price indices and drop the benchmark's dividend yield.
_BENCHMARK_SYMBOLS = {
    ListingExchange.US: "SPY",
    ListingExchange.LONDON: "CUKX.L",
}
_REFERENCE_INDEX_SYMBOLS = {
    ListingExchange.US: "^GSPC",
    ListingExchange.LONDON: "^FTSE",
}
# NYSE and Nasdaq share one session schedule (holidays, early closes, 16:00 New York close), so
# a single US calendar serves both listing venues.
_MARKET_EXCHANGES = {
    "s&p 500": ListingExchange.US,
    "xnys": ListingExchange.US,
    "xnas": ListingExchange.US,
    "ftse 100": ListingExchange.LONDON,
    "xlon": ListingExchange.LONDON,
}


def resolve_exchange(market: str | None) -> ListingExchange | None:
    """Map company listing metadata to a supported calendar; None means unresolved."""

    if market is None:
        return None
    return _MARKET_EXCHANGES.get(market.strip().casefold())


def benchmark_symbol(exchange: ListingExchange) -> str:
    """The benchmark whose adjusted closes `mr-v1` subtracts from the stock's."""

    return _BENCHMARK_SYMBOLS[exchange]


def reference_index_symbol(exchange: ListingExchange) -> str:
    """The headline price index for the same market: validation/reference only, never the input."""

    return _REFERENCE_INDEX_SYMBOLS[exchange]


@dataclass(frozen=True)
class SessionCalendar:
    exchange: ListingExchange
    sessions: tuple[date, ...]
    closes: tuple[datetime, ...]

    def __post_init__(self) -> None:
        if len(self.sessions) != len(self.closes):
            raise ValueError("sessions and closes must align")
        if any(
            later <= earlier for earlier, later in zip(self.closes, self.closes[1:], strict=False)
        ):
            raise ValueError("session closes must be strictly increasing")

    @property
    def timezone(self) -> ZoneInfo:
        return _EXCHANGE_TIMEZONES[self.exchange]

    def index_of(self, session: date) -> int | None:
        position = bisect_right(self.sessions, session) - 1
        if position >= 0 and self.sessions[position] == session:
            return position
        return None

    def assign_instant(self, published_at: datetime) -> int | None:
        """Index of the first session closing strictly after `published_at`.

        An article stamped exactly at the close cannot have moved that close, so it rolls to the
        next session. None means the instant falls outside the calendar's bounds.
        """

        if published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        instant = published_at.astimezone(UTC)
        if not self.closes or instant < self._coverage_start():
            return None
        position = bisect_right(self.closes, instant)
        return position if position < len(self.closes) else None

    def assign_date_only(self, published_date: date) -> int | None:
        """Conservative rule for a date with no trustworthy time-of-day.

        The article is treated as published at the end of that exchange-local day, so it can never
        be credited to a session whose close it might actually have followed.
        """

        end_of_local_day = datetime.combine(
            published_date + timedelta(days=1), time(0, 0), tzinfo=self.timezone
        )
        return self.assign_instant(end_of_local_day)

    def _coverage_start(self) -> datetime:
        # Before the first session's local midnight, an earlier unlisted session may have been
        # the correct assignment, so the calendar declines to answer.
        return datetime.combine(self.sessions[0], time(0, 0), tzinfo=self.timezone).astimezone(UTC)


def build_session_calendar(exchange: ListingExchange, start: date, end: date) -> SessionCalendar:
    """Materialise sessions and closes between two explicit dates (offline; bundled rules)."""

    import exchange_calendars
    import pandas as pd

    if end < start:
        raise ValueError("calendar end precedes start")
    calendar = exchange_calendars.get_calendar(
        exchange.value, start=pd.Timestamp(start), end=pd.Timestamp(end)
    )
    closes = calendar.closes
    return SessionCalendar(
        exchange=exchange,
        sessions=tuple(index.date() for index in closes.index),
        closes=tuple(value.tz_convert("UTC").to_pydatetime() for value in closes),
    )
