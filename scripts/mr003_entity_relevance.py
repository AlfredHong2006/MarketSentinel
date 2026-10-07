"""MR-003 follow-up: outcome-blind entity-relevance investigation.

Applies candidate deterministic article filters *before* session signals are built, recomputes the
pooled thresholds from sentiment only, and reports event counts per regime. The engine is run only
so that exclusivity and price-resolution *status* can be counted; no return, mean, CI or evidence
state is read or written, and the face-validity sample prints headlines and sentiment only.

Database read-only (`mode=ro`); prices come from the frozen fixture; no network, no LLM.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from mr003_preregister_thresholds import load_articles  # noqa: E402

from marketsentinel.market_reaction import PriceObservation, analyze_market_reaction  # noqa: E402
from marketsentinel.market_reaction.calendars import build_session_calendar  # noqa: E402
from marketsentinel.market_reaction.models import ListingExchange  # noqa: E402
from marketsentinel.market_reaction.signal import (  # noqa: E402
    build_session_signals,
    select_thresholds,
)
from marketsentinel.market_reaction.statistics import bootstrap_seed  # noqa: E402
from marketsentinel.subject_principal import reads_as_third_party_appointment  # noqa: E402

IDENTITIES = {
    "NVDA": SimpleNamespace(symbol="NVDA", name="NVIDIA Corporation"),
    "PFE": SimpleNamespace(symbol="PFE", name="Pfizer Inc."),
    "AAPL": SimpleNamespace(symbol="AAPL", name="Apple Inc."),
    "MSFT": SimpleNamespace(symbol="MSFT", name="Microsoft Corporation"),
}
VALIDATED = ("NVDA", "PFE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corpus = load_articles(args.db)
    connection = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)
    relevance = dict(connection.execute("SELECT fingerprint, relevance_score FROM articles"))
    payload = json.loads(args.prices.read_text(encoding="utf-8"))

    def prices(symbol: str) -> list[PriceObservation]:
        return [
            PriceObservation(date=date.fromisoformat(i["date"]), adjusted_close=i["adj_close"])
            for i in payload["prices"][symbol]
        ]

    all_dates = [
        (a.published_at.date() if a.published_at else a.published_date)
        for items in corpus.values()
        for a in items
    ]
    calendar = build_session_calendar(
        ListingExchange.US, min(all_dates) - timedelta(days=14), max(all_dates) + timedelta(days=35)
    )

    filters = {
        "V0_none": lambda t, a: True,
        "V1_not_third_party_appointment": lambda t, a: (
            not reads_as_third_party_appointment(a.title, IDENTITIES[t])
        ),
        "V2_relevance_ge_0.95": lambda t, a: relevance[a.article_id] >= 0.95,
        "V3_V1_and_V2": lambda t, a: (
            relevance[a.article_id] >= 0.95
            and not reads_as_third_party_appointment(a.title, IDENTITIES[t])
        ),
    }

    out: dict = {}
    for name, keep in filters.items():
        kept = {t: [a for a in items if keep(t, a)] for t, items in corpus.items()}
        signals = {t: build_session_signals(kept[t], t, calendar).signals for t in kept}
        pooled = [s for t in signals for s in signals[t]]
        selection = select_thresholds(pooled)
        entry: dict = {
            "articles_kept": {t: [len(kept[t]), len(corpus[t])] for t in kept},
            "population_E": selection.population_count,
            "tau_positive": selection.thresholds.positive,
            "tau_negative": selection.thresholds.negative,
            "raw_positive": selection.raw_positive,
            "raw_negative": selection.raw_negative,
            "positive_tail_share": selection.positive_tail_share,
            "negative_tail_share": selection.negative_tail_share,
            "companies": {},
        }
        for ticker in VALIDATED:
            result = analyze_market_reaction(
                ticker=ticker,
                listing_market="S&P 500",
                articles=kept[ticker],
                stock_prices=prices(ticker),
                benchmark_prices=prices("SPY"),
                thresholds=selection.thresholds,
                calendar=calendar,
            )
            # Status counts only: no return value is read from the result.
            entry["companies"][ticker] = {
                "history": {
                    "span": result.history.span_sessions,
                    "density": round(result.history.signal_density, 3),
                    "qualified": result.history.qualified_signal_session_count,
                    "sufficient": result.history.sufficient,
                },
                "signal_sessions": result.signal_session_count,
                "lagged_signal_sessions": result.data_quality.lagged_signal_session_count,
                **{
                    regime: {
                        "qualifying": r.qualifying_event_count,
                        "after_exclusivity": r.qualifying_event_count - r.suppressed_event_count,
                        "resolved": r.resolved_event_count,
                        "exact_timing": r.exact_timing_event_count,
                    }
                    for regime, r in (("negative", result.negative), ("positive", result.positive))
                },
            }
            if name == "V1_not_third_party_appointment":
                out.setdefault("_events_for_sampling", {})[ticker] = result
        out[name] = entry

    # Face validity: headlines and sentiment only, 3 events per company and regime.
    sample = []
    for ticker, result in out.pop("_events_for_sampling").items():
        for regime_name, regime in (("negative", result.negative), ("positive", result.positive)):
            events = [e for e in regime.events if e.status.value == "resolved"]
            generator = np.random.default_rng(
                int.from_bytes(
                    bootstrap_seed("entity", ticker, regime_name).to_bytes(8, "big")[:4], "big"
                )
            )
            for pick in sorted(int(p) for p in generator.permutation(len(events))[:3]):
                event = events[pick]
                headlines = [
                    connection.execute(
                        "SELECT title, source FROM articles WHERE fingerprint = ?", (i,)
                    ).fetchone()
                    for i in event.article_ids[:5]
                ]
                sample.append(
                    {
                        "ticker": ticker,
                        "regime": regime_name,
                        "session": event.session.isoformat(),
                        "signal": round(event.signal, 3),
                        "articles": event.article_count,
                        "article_ids": [i[:12] for i in event.article_ids[:5]],
                        "headlines": [list(h) for h in headlines],
                    }
                )
    out["face_validity_V1_headlines_only"] = sample
    connection.close()
    args.output.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
