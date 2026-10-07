"""Pins the MR-003 tracker-price fixture (adjusted closes for NVDA, PFE, SPY, CUKX.L).

Offline: reads only the frozen fixture and bundled calendar rules. It asserts structure and
alignment, never a return outcome, so it cannot become a place where results are tuned.
`CUKX.L` is a calendar-alignment check only; it does not validate the London path on real articles.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from marketsentinel.market_reaction import ListingExchange, PriceObservation
from marketsentinel.market_reaction.calendars import benchmark_symbol, build_session_calendar
from marketsentinel.market_reaction.returns import align_prices

FIXTURE = Path(__file__).parent / "fixtures" / "market_reaction" / "nvda_pfe_tracker_prices.json"
SYMBOLS = ("NVDA", "PFE", "SPY", "CUKX.L")


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _dates(payload: dict, symbol: str) -> list[date]:
    return [date.fromisoformat(item["date"]) for item in payload["prices"][symbol]]


def test_fixture_carries_the_tracker_symbols_and_no_price_indices(payload: dict) -> None:
    assert tuple(payload["prices"]) == SYMBOLS
    assert "^GSPC" not in payload["prices"] and "^FTSE" not in payload["prices"]
    assert benchmark_symbol(ListingExchange.US) == "SPY"
    assert benchmark_symbol(ListingExchange.LONDON) == "CUKX.L"


def test_series_are_sorted_unique_finite_and_long_enough(payload: dict) -> None:
    for symbol in SYMBOLS:
        dates = _dates(payload, symbol)
        assert dates == sorted(set(dates))
        assert len(dates) >= 700
        assert all(item["adj_close"] > 0 for item in payload["prices"][symbol])


def test_us_stocks_and_spy_share_one_session_calendar(payload: dict) -> None:
    assert _dates(payload, "NVDA") == _dates(payload, "SPY") == _dates(payload, "PFE")


def test_us_series_sit_exactly_on_the_xnys_calendar(payload: dict) -> None:
    calendar = build_session_calendar(ListingExchange.US, date(2023, 10, 1), date(2026, 10, 7))
    for symbol in ("NVDA", "PFE", "SPY"):
        aligned = align_prices(
            [
                PriceObservation(date=date.fromisoformat(i["date"]), adjusted_close=i["adj_close"])
                for i in payload["prices"][symbol]
            ],
            calendar,
        )
        assert aligned.off_calendar_dates == 0
        assert aligned.invalid_observations == 0
        assert aligned.gap_sessions() == 0


def test_cukx_dates_are_xlon_sessions_calendar_alignment_only(payload: dict) -> None:
    calendar = build_session_calendar(ListingExchange.LONDON, date(2023, 10, 1), date(2026, 10, 7))
    assert set(_dates(payload, "CUKX.L")) <= set(calendar.sessions)


def test_fixture_records_its_provenance_and_carries_no_secrets(payload: dict) -> None:
    meta = payload["_meta"]
    assert "auto_adjust=True" in meta["source"]
    assert meta["fetched_at"]
    text = FIXTURE.read_text(encoding="utf-8").casefold()
    for marker in ("http://", "https://", "api_key", "secret", "token="):
        assert marker not in text
