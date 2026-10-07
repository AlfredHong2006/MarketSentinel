"""MR-003 Phase 1: pre-register the two `mr-v1` regime thresholds from sentiment marginals only.

Outcome-blind by construction:

* the database is opened read-only (SQLite URI ``mode=ro``) and only ``articles`` / ``sentiments``
  are read;
* the session calendar is built from article dates alone, never from a price series;
* this module imports no price provider and no return code path, and it refuses to run if
  ``yfinance`` has already been imported;
* ``select_thresholds`` accepts session signals and nothing else.

Run this *before* ``scripts/mr003_fetch_prices.py`` and ``scripts/mr003_validate.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from marketsentinel.market_reaction.calendars import build_session_calendar
from marketsentinel.market_reaction.models import ListingExchange, ReactionArticle
from marketsentinel.market_reaction.signal import (
    build_session_signals,
    select_thresholds,
    split_publication_stamp,
)

_QUERY = """
    SELECT a.fingerprint, a.ticker, a.title, a.url, a.source, a.published_at, a.is_demo,
           s.positive, s.negative, s.neutral
    FROM articles a JOIN sentiments s ON s.article_fingerprint = a.fingerprint
    WHERE a.is_demo = 0
    ORDER BY a.ticker, a.published_at, a.fingerprint
"""
# NVDA and PFE are S&P 500 listings (MR-001 section 5); US calendar.
_EXCHANGE = ListingExchange.US


def load_articles(db_path: Path) -> dict[str, list[ReactionArticle]]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        grouped: dict[str, list[ReactionArticle]] = {}
        for row in connection.execute(_QUERY):
            published_at, published_date = split_publication_stamp(datetime.fromisoformat(row[5]))
            grouped.setdefault(row[1], []).append(
                ReactionArticle(
                    article_id=row[0],
                    ticker=row[1],
                    title=row[2],
                    url=row[3],
                    source=row[4],
                    published_at=published_at,
                    published_date=published_date,
                    p_positive=row[7],
                    p_negative=row[8],
                    p_neutral=row[9],
                    is_demo=bool(row[6]),
                )
            )
        return grouped
    finally:
        connection.close()


def _sufficiency(signals, calendar) -> dict:
    # Mirrors the spec's three conditions using signals and the calendar only.
    from marketsentinel.market_reaction.models import (
        MIN_DISTINCT_SOURCES,
        MIN_HISTORY_SESSIONS,
        MIN_QUALIFIED_SIGNAL_SESSIONS,
        MIN_SIGNAL_DENSITY,
    )

    if not signals:
        return {"sufficient": False}
    span = calendar.index_of(signals[-1].session) - calendar.index_of(signals[0].session)
    qualified = sum(s.distinct_source_count >= MIN_DISTINCT_SOURCES for s in signals)
    density = len(signals) / (span + 1)
    return {
        "span": span,
        "signal_sessions": len(signals),
        "density": round(density, 4),
        "qualified": qualified,
        "sufficient": span >= MIN_HISTORY_SESSIONS
        and density >= MIN_SIGNAL_DENSITY
        and qualified >= MIN_QUALIFIED_SIGNAL_SESSIONS,
    }


def _selection_dict(selection) -> dict:
    return {
        "tau_negative": selection.thresholds.negative,
        "tau_positive": selection.thresholds.positive,
        "raw_negative": selection.raw_negative,
        "raw_positive": selection.raw_positive,
        "negative_floor_applied": selection.negative_floor_applied,
        "positive_floor_applied": selection.positive_floor_applied,
        "negative_tail_share": selection.negative_tail_share,
        "positive_tail_share": selection.positive_tail_share,
        "population_E": selection.population_count,
        "pooled_session_count": selection.pooled_session_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "yfinance" in sys.modules:
        raise SystemExit("refusing to run: a price provider is already loaded")

    grouped = load_articles(args.db)
    dates = [
        (a.published_at.date() if a.published_at else a.published_date)
        for items in grouped.values()
        for a in items
    ]
    calendar = build_session_calendar(
        _EXCHANGE, min(dates) - timedelta(days=14), max(dates) + timedelta(days=35)
    )

    per_company: dict[str, dict] = {}
    all_signals = []
    sufficient_signals = []
    for ticker, items in sorted(grouped.items()):
        built = build_session_signals(items, ticker, calendar)
        sufficiency = _sufficiency(built.signals, calendar)
        qualified = [s for s in built.signals if s.distinct_source_count >= 3]
        per_company[ticker] = {
            "articles": len(items),
            "signal_sessions": len(built.signals),
            "E_contribution": len(qualified),
            "sufficiency": sufficiency,
        }
        all_signals.extend(built.signals)
        if sufficiency["sufficient"]:
            sufficient_signals.extend(built.signals)

    pooled_all = select_thresholds(all_signals)
    pooled_sufficient = select_thresholds(sufficient_signals)
    result = {
        "db_snapshot_mtime_utc": datetime.fromtimestamp(os.path.getmtime(args.db), UTC).isoformat(),
        "computed_at_utc": datetime.now(UTC).isoformat(),
        "calendar": {
            "exchange": _EXCHANGE.value,
            "first": str(calendar.sessions[0]),
            "last": str(calendar.sessions[-1]),
        },
        "per_company": per_company,
        "all_companies": _selection_dict(pooled_all),
        "history_sufficient_only": _selection_dict(pooled_sufficient),
        "rounded_thresholds_agree": pooled_all.thresholds == pooled_sufficient.thresholds,
        "price_modules_loaded": "yfinance" in sys.modules,
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
