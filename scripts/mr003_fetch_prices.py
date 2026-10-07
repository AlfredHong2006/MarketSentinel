"""MR-003 Phase 2: one-off NETWORK fetch of adjusted closes, frozen into a test fixture.

Uses the existing price path (`YFinancePriceProvider`, `auto_adjust=True`), so the series are
split/dividend-adjusted exactly as the product's are. Run once, by hand, after
`scripts/mr003_preregister_thresholds.py`. Tests never run this; they read the fixture.

Only date and adjusted close are written. Adjusted levels are rewritten backwards at every
dividend, so the fixture is a snapshot: compare returns, never levels, across fetches.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from marketsentinel.domain import Constituent
from marketsentinel.sources.prices import YFinancePriceProvider

# (symbol, role). CUKX.L is fetched for calendar/benchmark-alignment checks only: no London-listed
# company has stored articles, so it does not validate the London path.
SYMBOLS = (
    ("NVDA", "stock"),
    ("PFE", "stock"),
    ("SPY", "us_benchmark"),
    ("CUKX.L", "london_benchmark_alignment_only"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    provider = YFinancePriceProvider(training_period="3y")
    series: dict[str, list[dict]] = {}
    fetched_at = None
    for symbol, _role in SYMBOLS:
        history = provider.fetch(
            Constituent(
                symbol=symbol,
                yahoo_symbol=symbol,
                name=symbol,
                market="FTSE 100" if symbol.endswith(".L") else "S&P 500",
            )
        )
        fetched_at = history.fetched_at
        series[symbol] = [
            {"date": point.date.isoformat(), "adj_close": round(point.close, 6)}
            for point in history.points
        ]
        print(symbol, len(series[symbol]), series[symbol][0]["date"], series[symbol][-1]["date"])

    payload = {
        "_meta": {
            "purpose": "mr-v1 MR-003 tracker price series (adjusted closes), frozen snapshot",
            "source": "Yahoo Finance via yfinance, auto_adjust=True (existing price path)",
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "roles": dict(SYMBOLS),
            "note": "Adjusted levels change with every later dividend; use returns only.",
        },
        "prices": series,
    }
    args.output.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
