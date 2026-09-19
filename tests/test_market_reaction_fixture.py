"""Pins the data assumptions the MR-001 real-data fixture makes for ``mr-v1`` (MR-002)."""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "market_reaction" / "nvda_pfe_real_sample.json"
PACIFIC = ZoneInfo("America/Los_Angeles")


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_articles_are_real_scored_and_uniquely_identified(fixture: dict) -> None:
    articles = fixture["articles"]
    assert 200 <= len(articles) <= 600
    assert len({item["article_id"] for item in articles}) == len(articles)
    assert {item["ticker"] for item in articles} == {"NVDA", "PFE"}
    for item in articles:
        assert item["is_demo"] is False
        assert item["title"] and item["source"]
        total = item["p_positive"] + item["p_negative"] + item["p_neutral"]
        assert total == pytest.approx(1.0, abs=0.01)
        assert -1.0 <= item["p_positive"] - item["p_negative"] <= 1.0


def test_articles_carry_no_urls_or_secrets(fixture: dict) -> None:
    text = FIXTURE.read_text(encoding="utf-8").casefold()
    for marker in ("http://", "https://", "api_key", "apikey", "secret", "token="):
        assert marker not in text


def test_articles_are_in_deterministic_order(fixture: dict) -> None:
    keys = [
        (item["ticker"], item["published_at"], item["article_id"]) for item in fixture["articles"]
    ]
    assert keys == sorted(keys)


def test_timestamps_are_aware_and_both_qualities_are_represented(fixture: dict) -> None:
    qualities = set()
    for item in fixture["articles"]:
        published = datetime.fromisoformat(item["published_at"])
        assert published.tzinfo is not None
        assert published <= datetime.fromisoformat(item["fetched_at"])
        qualities.add(item["timestamp_quality"])
        local = published.astimezone(PACIFIC)
        is_midnight = (local.hour, local.minute, local.second) == (0, 0, 0) or (
            published.hour,
            published.minute,
            published.second,
        ) == (0, 0, 0)
        assert (item["timestamp_quality"] == "date_only") == is_midnight
    assert qualities == {"full", "date_only"}


def test_date_only_stamps_cover_both_sides_of_us_dst(fixture: dict) -> None:
    # Midnight Pacific is 07:00 UTC in PDT and 08:00 UTC in PST; a hardcoded hour would miss one.
    hours = {
        datetime.fromisoformat(item["published_at"]).hour
        for item in fixture["articles"]
        if item["timestamp_quality"] == "date_only"
    }
    assert {7, 8} <= hours


def test_price_series_are_real_sessions(fixture: dict) -> None:
    prices = fixture["prices"]
    assert set(prices) == {"NVDA", "PFE", "^GSPC", "^FTSE"}
    for rows in prices.values():
        dates = [date.fromisoformat(row["date"]) for row in rows]
        assert dates == sorted(set(dates))
        assert all(day.weekday() < 5 for day in dates)
        assert all(row["adj_close"] > 0 for row in rows)


def test_us_stock_and_benchmark_share_session_dates(fixture: dict) -> None:
    prices = fixture["prices"]
    us_dates = [row["date"] for row in prices["^GSPC"]]
    assert [row["date"] for row in prices["NVDA"]] == us_dates
    assert [row["date"] for row in prices["PFE"]] == us_dates


def test_us_and_london_calendars_diverge_on_real_holidays(fixture: dict) -> None:
    us_dates = {row["date"] for row in fixture["prices"]["^GSPC"]}
    london_dates = {row["date"] for row in fixture["prices"]["^FTSE"]}
    # Thanksgiving 2025 and Labor Day 2026: US closed, London open.
    for holiday in ("2025-11-27", "2026-09-07"):
        assert holiday not in us_dates
        assert holiday in london_dates
    # Early May bank holiday 2026: London closed, US open.
    assert "2026-05-04" in us_dates
    assert "2026-05-04" not in london_dates


def test_prices_cover_the_full_reaction_path_after_the_last_article(fixture: dict) -> None:
    us_dates = [row["date"] for row in fixture["prices"]["^GSPC"]]
    last_article_day = max(item["published_at"][:10] for item in fixture["articles"])
    assert sum(day > last_article_day for day in us_dates) == 5
