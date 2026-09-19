"""MR-001 data-readiness inspection for Historical Market Reaction V1 (``mr-v1``).

A research/inspection CLI, not the ``mr-v1`` engine (that is MR-002's). It answers "can the stored
data support the approved methodology honestly?" and builds the small real-data fixture MR-002
tests against.

Subcommands:

``report``          offline, read-only. Reads the SQLite database (opened ``mode=ro``) and a
                    directory of price CSV snapshots, prints the readiness numbers as JSON.
``build-fixture``   offline, read-only. Writes the deterministic fixture JSON from the same inputs.
``fetch-prices``    NETWORK (yfinance, read-only GETs). Writes the price CSV snapshots the other two
                    subcommands consume. Never touches the database.

Return outcomes are deliberately never computed here: ``tau`` candidates come from pooled
session-sentiment marginals only, as the methodology requires.

Session assignment below is an *estimate* good enough for counting: regular 16:00 New York closes,
with real trading dates taken from the price snapshot. It ignores early closes. The real
exchange-calendar implementation belongs to MR-002.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TICKERS = ("NVDA", "PFE", "AAPL", "MSFT", "AMZN")
PRICE_SYMBOLS = ("NVDA", "PFE", "AAPL", "MSFT", "AMZN", "^GSPC", "^FTSE")
NEW_YORK = ZoneInfo("America/New_York")
PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = ZoneInfo("UTC")
REGULAR_CLOSE = time(16, 0)

MIN_TAU = 0.20
TAIL_TARGET = 0.15
MIN_DISTINCT_SOURCES = 3
PRIMARY_HORIZON = 5
PATH_HORIZON = 10
MIN_HISTORY_SESSIONS = 126

FIXTURE_TICKERS = ("NVDA", "PFE")
# Two windows chosen for what they exercise, not for what the market did in them:
#  - the first is date-only-heavy backfill spanning the 2025-11-02 US DST change and Thanksgiving
#    (closed 2025-11-27, early close 2025-11-28);
#  - the second is live full-timestamp ingestion spanning Labor Day (closed 2026-09-07).
FIXTURE_ARTICLE_WINDOWS = (
    (date(2025, 10, 27), date(2025, 12, 1)),
    (date(2026, 8, 17), date(2026, 9, 13)),
)
FIXTURE_PRICE_START = date(2025, 10, 1)
FIXTURE_MAX_ARTICLES_PER_TICKER_DAY = 6


def _price_file(prices_dir: Path, symbol: str) -> Path:
    return prices_dir / f"px_{symbol.replace('^', 'IDX_')}.csv"


def load_prices(prices_dir: Path, symbol: str) -> list[dict]:
    path = _price_file(prices_dir, symbol)
    if not path.exists():
        return []
    rows = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                {
                    "date": date.fromisoformat(row["ts"][:10]),
                    "adj_close": float(row["adj_close_autoadjust"]),
                    "raw_close": float(row["raw_close"]),
                    "dividend": float(row["dividends"] or 0.0),
                    "split": float(row["splits"] or 0.0),
                }
            )
    return rows


def load_articles(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            """
            SELECT a.fingerprint, a.ticker, a.title, a.normalized_title, a.source, a.provider,
                   a.published_at, a.fetched_at, a.is_demo,
                   s.positive, s.negative, s.neutral, s.model_name
            FROM articles a JOIN sentiments s ON s.article_fingerprint = a.fingerprint
            ORDER BY a.ticker, a.published_at, a.fingerprint
            """
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor]
    finally:
        connection.close()


def timestamp_quality(article: dict) -> str:
    """``full`` / ``date_only`` / ``unusable``.

    The Google News historical-range feed stamps most entries at exactly midnight Pacific
    (07:00 or 08:00 UTC depending on US DST). That is a date, not a time of day.
    """
    try:
        published = datetime.fromisoformat(article["published_at"])
        fetched = datetime.fromisoformat(article["fetched_at"])
    except (TypeError, ValueError):
        return "unusable"
    if published.tzinfo is None or published > fetched + timedelta(minutes=5):
        return "unusable"
    local = published.astimezone(PACIFIC)
    if (local.hour, local.minute, local.second) == (0, 0, 0):
        return "date_only"
    if (published.hour, published.minute, published.second) == (0, 0, 0):
        return "date_only"
    return "full"


def valid_sentiment(article: dict) -> bool:
    values = (article["positive"], article["negative"], article["neutral"])
    if any(value is None or not 0.0 <= value <= 1.0 for value in values):
        return False
    return abs(sum(values) - 1.0) <= 0.01


def assign_session(article: dict, quality: str, sessions: list[date]) -> date | None:
    """First session whose (regular) close is after publication; date-only -> end of local day."""
    published = datetime.fromisoformat(article["published_at"])
    if quality == "date_only":
        # Conservative rule from the methodology: end of local day, so strictly after that date.
        index = bisect.bisect_right(sessions, published.astimezone(PACIFIC).date())
    else:
        local = published.astimezone(NEW_YORK)
        index = bisect.bisect_left(sessions, local.date())
        if (
            index < len(sessions)
            and sessions[index] == local.date()
            and local.time() >= REGULAR_CLOSE
        ):
            index += 1
    return sessions[index] if index < len(sessions) else None


def build_sessions(articles: list[dict], sessions: list[date]) -> tuple[list[dict], Counter]:
    """Eligible, title-deduplicated session signals for one ticker."""
    quality_counts: Counter = Counter()
    by_session: dict[date, dict[str, dict]] = defaultdict(dict)
    for article in articles:
        quality = timestamp_quality(article)
        quality_counts[quality] += 1
        if article["is_demo"] or quality == "unusable" or not valid_sentiment(article):
            continue
        session = assign_session(article, quality, sessions)
        if session is None:
            quality_counts["after_last_price_session"] += 1
            continue
        # Earliest article wins a same-session title collision (rows arrive published_at-ordered).
        by_session[session].setdefault(article["normalized_title"], {**article, "quality": quality})
    result = []
    for session in sorted(by_session):
        kept = list(by_session[session].values())
        result.append(
            {
                "session": session,
                "signal": statistics.fmean(item["positive"] - item["negative"] for item in kept),
                "articles": len(kept),
                "sources": len({item["source"] for item in kept}),
                "date_only_share": sum(item["quality"] == "date_only" for item in kept) / len(kept),
            }
        )
    return result, quality_counts


def exclusive_events(candidates: list[dict], sessions: list[date]) -> list[dict]:
    """Keep the first event; suppress same-regime events whose +5 window overlaps a kept one."""
    position = {session: index for index, session in enumerate(sessions)}
    kept: list[dict] = []
    for candidate in candidates:
        if (
            kept
            and position[candidate["session"]] - position[kept[-1]["session"]] <= PRIMARY_HORIZON
        ):
            continue
        kept.append(candidate)
    return kept


def quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))]


def regime_counts(signals: list[dict], sessions: list[date], tau: float) -> dict:
    position = {session: index for index, session in enumerate(sessions)}
    output = {}
    for name, test in (
        ("clearly_negative", lambda value: value <= -tau),
        ("clearly_positive", lambda value: value >= tau),
    ):
        in_tail = [item for item in signals if test(item["signal"])]
        qualifying = [item for item in in_tail if item["sources"] >= MIN_DISTINCT_SOURCES]
        kept = exclusive_events(qualifying, sessions)
        output[name] = {
            "tail_sessions": len(in_tail),
            "with_3_sources": len(qualifying),
            "after_exclusivity": len(kept),
            "resolved_plus5": sum(
                position[item["session"]] + PRIMARY_HORIZON < len(sessions) for item in kept
            ),
            "resolved_plus10": sum(
                position[item["session"]] + PATH_HORIZON < len(sessions) for item in kept
            ),
        }
    return output


def price_summary(rows: list[dict]) -> dict:
    if not rows:
        return {"available": False}
    dates = [row["date"] for row in rows]
    return {
        "available": True,
        "sessions": len(rows),
        "first": dates[0].isoformat(),
        "last": dates[-1].isoformat(),
        "splits": {row["date"].isoformat(): row["split"] for row in rows if row["split"]},
        "dividend_count": sum(row["dividend"] > 0 for row in rows),
        # >1 at the start of the window only if dividends were folded into the adjusted series.
        "first_raw_over_adjusted": round(rows[0]["raw_close"] / rows[0]["adj_close"], 6),
        "max_abs_1d_adjusted_return": round(
            max(
                abs(current["adj_close"] / previous["adj_close"] - 1.0)
                for previous, current in zip(rows, rows[1:], strict=False)
            ),
            4,
        ),
    }


def command_report(args: argparse.Namespace) -> None:
    articles = load_articles(args.db)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for article in articles:
        by_ticker[article["ticker"]].append(article)

    report: dict = {"tickers": {}, "prices": {}}
    for symbol in PRICE_SYMBOLS:
        report["prices"][symbol] = price_summary(load_prices(args.prices_dir, symbol))

    signals_by_ticker: dict[str, list[dict]] = {}
    sessions_by_ticker: dict[str, list[date]] = {}
    for ticker in TICKERS:
        rows = by_ticker.get(ticker, [])
        sessions = [row["date"] for row in load_prices(args.prices_dir, ticker)]
        signals, quality = build_sessions(rows, sessions)
        signals_by_ticker[ticker] = signals
        sessions_by_ticker[ticker] = sessions
        real = [row for row in rows if not row["is_demo"]]
        total = max(1, len(real))
        span_sessions = 0
        if signals:
            span_sessions = (
                sessions.index(signals[-1]["session"]) - sessions.index(signals[0]["session"]) + 1
            )
        report["tickers"][ticker] = {
            "real_scored_articles": len(real),
            "first_published": real[0]["published_at"] if real else None,
            "last_published": real[-1]["published_at"] if real else None,
            "timestamp_quality_pct": {
                key: round(100.0 * quality.get(key, 0) / total, 1)
                for key in ("full", "date_only", "unusable")
            },
            "invalid_sentiment_rows": sum(not valid_sentiment(row) for row in real),
            "signal_sessions": len(signals),
            "signal_sessions_with_3_sources": sum(
                item["sources"] >= MIN_DISTINCT_SOURCES for item in signals
            ),
            "sessions_first_to_last_signal": span_sessions,
            "meets_126_session_history": span_sessions >= MIN_HISTORY_SESSIONS,
            "median_articles_per_signal_session": (
                statistics.median(item["articles"] for item in signals) if signals else 0
            ),
            "signal_sessions_majority_date_only": sum(
                item["date_only_share"] > 0.5 for item in signals
            ),
        }

    pooled = [item["signal"] for values in signals_by_ticker.values() for item in values]
    lower, upper = quantile(pooled, TAIL_TARGET), quantile(pooled, 1.0 - TAIL_TARGET)
    unconstrained = (abs(lower) + abs(upper)) / 2.0
    report["pooled_session_signal"] = {
        "sessions": len(pooled),
        "quantiles": {
            str(q): round(quantile(pooled, q), 4) for q in (0.05, 0.15, 0.25, 0.5, 0.75, 0.85, 0.95)
        },
        "mean": round(statistics.fmean(pooled), 4) if pooled else None,
        "symmetric_tau_for_15pct_tails_unconstrained": round(unconstrained, 4),
        "tau_after_floor": round(max(MIN_TAU, unconstrained), 4),
        "note": "marginals only; no return was read to produce any number in this block",
    }
    for tau in sorted({MIN_TAU, round(max(MIN_TAU, unconstrained), 2), 0.30}):
        block = {
            "pooled_share_negative_tail": round(
                sum(value <= -tau for value in pooled) / max(1, len(pooled)), 3
            ),
            "pooled_share_positive_tail": round(
                sum(value >= tau for value in pooled) / max(1, len(pooled)), 3
            ),
            "per_ticker": {
                ticker: regime_counts(signals_by_ticker[ticker], sessions_by_ticker[ticker], tau)
                for ticker in TICKERS
            },
        }
        report.setdefault("event_counts_by_tau", {})[f"{tau:.2f}"] = block
    print(json.dumps(report, indent=2, default=str))


def command_build_fixture(args: argparse.Namespace) -> None:
    articles = load_articles(args.db)
    selected: list[dict] = []
    per_day: Counter = Counter()
    for article in articles:
        if article["ticker"] not in FIXTURE_TICKERS or article["is_demo"]:
            continue
        published = datetime.fromisoformat(article["published_at"])
        day = published.astimezone(UTC).date()
        if not any(start <= day <= end for start, end in FIXTURE_ARTICLE_WINDOWS):
            continue
        key = (article["ticker"], day)
        if per_day[key] >= FIXTURE_MAX_ARTICLES_PER_TICKER_DAY:
            continue
        per_day[key] += 1
        selected.append(
            {
                "article_id": article["fingerprint"],
                "ticker": article["ticker"],
                "title": article["title"],
                "normalized_title": article["normalized_title"],
                "source": article["source"],
                "provider": article["provider"],
                "published_at": article["published_at"],
                "fetched_at": article["fetched_at"],
                "timestamp_quality": timestamp_quality(article),
                "is_demo": False,
                "p_positive": article["positive"],
                "p_negative": article["negative"],
                "p_neutral": article["neutral"],
                "sentiment_model": article["model_name"],
            }
        )

    prices = {}
    for symbol in (*FIXTURE_TICKERS, "^GSPC", "^FTSE"):
        prices[symbol] = [
            {"date": row["date"].isoformat(), "adj_close": round(row["adj_close"], 6)}
            for row in load_prices(args.prices_dir, symbol)
            if row["date"] >= FIXTURE_PRICE_START
        ]

    fixture = {
        "_meta": {
            "purpose": "MR-001 real-data fixture for mr-v1 (MR-002) offline tests",
            "frozen": "research snapshot; the live database and adjusted price levels keep moving",
            "article_source": "data/marketsentinel.db (articles JOIN sentiments), read-only",
            "article_windows": [[a.isoformat(), b.isoformat()] for a, b in FIXTURE_ARTICLE_WINDOWS],
            "article_sampling": (
                f"first {FIXTURE_MAX_ARTICLES_PER_TICKER_DAY} articles per ticker per UTC day, "
                "ordered by (published_at, fingerprint)"
            ),
            "price_source": "yfinance 1d history, auto_adjust=True (split+dividend adjusted close)",
            "price_snapshot_date": args.snapshot_date,
            "benchmarks": {"S&P 500": "^GSPC (price index)", "FTSE 100": "^FTSE (price index)"},
            "listing": {"NVDA": "US", "PFE": "US"},
            "timestamp_quality_rule": (
                "date_only = exactly 00:00:00 America/Los_Angeles (Google News historical-range "
                "stamp) or 00:00:00 UTC; everything else full"
            ),
            "labels": "none; this fixture carries no expected mr-v1 outputs",
        },
        "articles": selected,
        "prices": prices,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(fixture, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(
        f"{len(selected)} articles, {sum(len(v) for v in prices.values())} price rows -> {args.output}"
    )


def command_fetch_prices(args: argparse.Namespace) -> None:
    import pandas as pd
    import yfinance as yf

    args.prices_dir.mkdir(parents=True, exist_ok=True)
    for symbol in PRICE_SYMBOLS:
        ticker = yf.Ticker(symbol)
        # Same call shape as sources/prices.py (auto_adjust=True), plus the unadjusted series and
        # corporate actions so the adjustment can be verified rather than assumed.
        adjusted = ticker.history(
            period="3y", interval="1d", auto_adjust=True, actions=False, timeout=15
        )
        raw = ticker.history(
            period="3y", interval="1d", auto_adjust=False, actions=True, timeout=15
        )
        frame = pd.DataFrame(
            {
                "adj_close_autoadjust": adjusted["Close"],
                "raw_close": raw["Close"],
                "raw_adj_close": raw.get("Adj Close"),
                "dividends": raw.get("Dividends"),
                "splits": raw.get("Stock Splits"),
                "volume": adjusted["Volume"],
            }
        )
        frame.index.name = "ts"
        frame.to_csv(_price_file(args.prices_dir, symbol))
        print(symbol, len(frame))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    report = commands.add_parser("report", help="offline readiness numbers as JSON")
    report.add_argument("--db", type=Path, default=Path("data/marketsentinel.db"))
    report.add_argument("--prices-dir", type=Path, required=True)
    report.set_defaults(handler=command_report)

    fixture = commands.add_parser("build-fixture", help="offline; write the MR-002 fixture")
    fixture.add_argument("--db", type=Path, default=Path("data/marketsentinel.db"))
    fixture.add_argument("--prices-dir", type=Path, required=True)
    fixture.add_argument("--snapshot-date", required=True, help="date the price CSVs were fetched")
    fixture.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures/market_reaction/nvda_pfe_real_sample.json"),
    )
    fixture.set_defaults(handler=command_build_fixture)

    fetch = commands.add_parser("fetch-prices", help="NETWORK: snapshot yfinance history to CSV")
    fetch.add_argument("--prices-dir", type=Path, required=True)
    fetch.set_defaults(handler=command_fetch_prices)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
