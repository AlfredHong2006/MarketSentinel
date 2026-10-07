"""MR-003 Phase 3: measure the `mr-v1` procedure on the real NVDA / PFE corpus.

Manual research script. It reads the live database read-only (SQLite `mode=ro`) for articles and
sentiment, and the frozen price fixture for adjusted closes. It makes no network call, no LLM call,
and no write. Thresholds are explicit arguments: they must come from
`scripts/mr003_preregister_thresholds.py`, never from a return.

`--regimes` limits which regime's *outcomes* are computed and emitted. A regime that is not
requested contributes nothing to the output beyond price-free event counts.

Everything measured here is a measurement of the procedure, not an assumed rate: the synthetic
null, the signal permutation, the fixed-n random-session placebo, the one-session lag shifts, the
split-half and ingestion-regime splits.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from mr003_preregister_thresholds import load_articles  # noqa: E402

from marketsentinel.market_reaction import (  # noqa: E402
    EvidenceState,
    PriceObservation,
    Regime,
    RegimeThresholds,
    analyze_market_reaction,
)
from marketsentinel.market_reaction.calendars import build_session_calendar  # noqa: E402
from marketsentinel.market_reaction.engine import _build_events  # noqa: E402
from marketsentinel.market_reaction.models import (  # noqa: E402
    MIN_EVENTS_PRELIMINARY,
    MIN_EVENTS_VERDICT,
    PRIMARY_HORIZON,
    EventStatus,
    ListingExchange,
)
from marketsentinel.market_reaction.returns import align_prices  # noqa: E402
from marketsentinel.market_reaction.statistics import (  # noqa: E402
    bootstrap_seed,
    regime_state,
    split_half_means,
    summarize_returns,
)

TICKERS = ("NVDA", "PFE")
ALIASES = {"NVDA": ("nvidia", "nvda"), "PFE": ("pfizer", "pfe")}
# Quote-page / aggregator titles (MR-001 section 7.8: "Pfizer Tokenized Stock price today ... to USD").
_JUNK_TITLE = re.compile(
    r"stock price today|share price today|price today|to usd|tokenized|live price|"
    r"price chart|stock quote|stock forecast|price prediction",
    re.IGNORECASE,
)
INGESTION_BREAK = date(2026, 8, 17)  # MR-001 section 7.3: backfill -> live ingestion
PLACEBO_DRAWS = 1000
SYNTHETIC_TRIALS = 2000


def _prices(payload: dict, symbol: str) -> list[PriceObservation]:
    return [
        PriceObservation(date=date.fromisoformat(i["date"]), adjusted_close=i["adj_close"])
        for i in payload["prices"][symbol]
    ]


def _state_of(values: list[float], seed: int) -> tuple[str, dict]:
    """Verdict state for chronological +5 values, computing the bootstrap only when n >= 20."""

    n = len(values)
    if n < MIN_EVENTS_PRELIMINARY:
        return EvidenceState.INSUFFICIENT_EVENTS.value, {"n": n}
    if n < MIN_EVENTS_VERDICT:
        return EvidenceState.PRELIMINARY.value, {"n": n}
    stats = summarize_returns(values, seed)
    first, second = split_half_means(values)
    state = regime_state(stats, first, second)
    passes = (stats.ci_low > 0 or stats.ci_high < 0) and abs(stats.mean) >= 0.005
    return state.value, {"n": n, "mean": stats.mean, "passes_ci_and_effect": passes}


def _resolved_values(events, regime: Regime) -> list[float]:
    return [
        e.primary_market_adjusted_return
        for e in events
        if e.regime is regime and e.status is EventStatus.RESOLVED
    ]


def _greedy_spaced(order: np.ndarray, count: int, spacing: int) -> list[int]:
    kept: list[int] = []
    for position in order:
        if all(abs(int(position) - other) >= spacing for other in kept):
            kept.append(int(position))
            if len(kept) == count:
                break
    return sorted(kept)


def synthetic_null(sigma: float, seed: int = 20260920) -> dict:
    """Detection rate of the verdict rule on iid mean-zero returns (and a known-effect control)."""

    generator = np.random.default_rng(seed)
    out: dict = {"sigma_used": sigma, "trials": SYNTHETIC_TRIALS}
    for label, draw in (
        ("normal", lambda n, mu: generator.normal(mu, sigma, n)),
        ("student_t3", lambda n, mu: mu + sigma / np.sqrt(3) * generator.standard_t(3, n)),
    ):
        for mu, tag in ((0.0, "null"), (0.02, "effect_2pct")):
            for n in (20, 30, 50):
                tally = Counter()
                for trial in range(SYNTHETIC_TRIALS):
                    state, _ = _state_of(list(draw(n, mu)), bootstrap_seed("syn", str(trial)))
                    tally[state] += 1
                out[f"{label}/{tag}/n={n}"] = {
                    "detected": tally["detected"] / SYNTHETIC_TRIALS,
                    "unstable": tally["unstable"] / SYNTHETIC_TRIALS,
                }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tau-negative", type=float, required=True)
    parser.add_argument("--tau-positive", type=float, required=True)
    parser.add_argument(
        "--regimes", choices=("none", "negative", "positive", "both"), required=True
    )
    args = parser.parse_args()
    wanted = {
        "none": [],
        "negative": [Regime.CLEARLY_NEGATIVE],
        "positive": [Regime.CLEARLY_POSITIVE],
        "both": [Regime.CLEARLY_NEGATIVE, Regime.CLEARLY_POSITIVE],
    }[args.regimes]
    thresholds = RegimeThresholds(negative=args.tau_negative, positive=args.tau_positive)

    payload = json.loads(args.prices.read_text(encoding="utf-8"))
    corpus = load_articles(args.db)
    connection = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True)

    output: dict = {
        "thresholds": thresholds.model_dump(),
        "regimes_emitted": [r.value for r in wanted],
        "companies": {},
    }
    sigma_pool: list[float] = []
    for ticker in TICKERS:
        articles = corpus[ticker]
        stock_list = _prices(payload, ticker)
        bench_list = _prices(payload, "SPY")
        dates = [p.date for p in (*stock_list, *bench_list)] + [
            (a.published_at.date() if a.published_at else a.published_date) for a in articles
        ]
        calendar = build_session_calendar(
            ListingExchange.US, min(dates) - timedelta(days=14), max(dates) + timedelta(days=35)
        )
        result = analyze_market_reaction(
            ticker=ticker,
            listing_market="S&P 500",
            articles=articles,
            stock_prices=stock_list,
            benchmark_prices=bench_list,
            thresholds=thresholds,
            calendar=calendar,
        )
        stock = align_prices(stock_list, calendar)
        bench = align_prices(bench_list, calendar)
        signals = list(result.session_signals)
        company: dict = {
            "history": result.history.model_dump(),
            "benchmark": result.benchmark_symbol,
            "company_state": result.state.value if result.state else None,
            "data_quality": result.data_quality.model_dump(),
            "spearman": result.spearman.model_dump() if result.spearman else None,
        }

        # Price-free funnel, both regimes, using only signals and event statuses.
        funnel = {}
        for regime, tau in (
            (Regime.CLEARLY_NEGATIVE, thresholds.negative),
            (Regime.CLEARLY_POSITIVE, thresholds.positive),
        ):
            sign = -1 if regime is Regime.CLEARLY_NEGATIVE else 1
            tail = [s for s in signals if sign * s.signal >= tau]
            qualified = [s for s in tail if s.distinct_source_count >= 3]
            regime_result = (
                result.negative if regime is Regime.CLEARLY_NEGATIVE else result.positive
            )
            funnel[regime.value] = {
                "tail_sessions": len(tail),
                "with_3_sources": len(qualified),
                "after_exclusivity": len(qualified) - regime_result.suppressed_event_count,
                "resolved_at_plus5": regime_result.resolved_event_count,
                "pending": regime_result.pending_event_count,
                "missing_price": regime_result.missing_price_event_count,
                "suppressed_overlap": regime_result.suppressed_event_count,
                "exact_timing_resolved": regime_result.day_zero_event_count,
            }
        company["funnel"] = funnel

        # Unconditional +5 market-adjusted returns over every session, used only as the
        # placebo universe and for the synthetic null's scale.
        universe = {
            i: stock.closes[i + 5] / stock.closes[i] - bench.closes[i + 5] / bench.closes[i]
            for i in sorted(stock.closes)
            if i + 5 in stock.closes and i in bench.closes and i + 5 in bench.closes
        }
        sigma_pool.append(float(np.std(list(universe.values()))))
        first_signal = calendar.index_of(signals[0].session)
        last_signal = calendar.index_of(signals[-1].session)
        in_span = [i for i in universe if first_signal <= i <= last_signal]
        company["unconditional_plus5_sigma"] = sigma_pool[-1]

        company["regimes"] = {}
        for regime in wanted:
            regime_result = (
                result.negative if regime is Regime.CLEARLY_NEGATIVE else result.positive
            )
            events = [e for e in regime_result.events]
            resolved = [e for e in events if e.status is EventStatus.RESOLVED]
            entry: dict = {
                "state": regime_result.state.value,
                "threshold": regime_result.threshold,
                "n": regime_result.resolved_event_count,
                "n_day0": regime_result.day_zero_event_count,
                "primary": regime_result.primary.model_dump() if regime_result.primary else None,
                "first_half_mean": regime_result.first_half_mean,
                "second_half_mean": regime_result.second_half_mean,
                "ci_excludes_zero": regime_result.ci_excludes_zero,
                "meets_minimum_effect": regime_result.meets_minimum_effect,
                "split_half_same_sign": regime_result.split_half_same_sign,
                "mean_median_same_sign": regime_result.mean_median_same_sign,
                "path": [
                    {
                        "h": p.horizon,
                        "cohort": p.cohort.value,
                        "n": p.statistics.n,
                        "mean": p.statistics.mean,
                        "median": p.statistics.median,
                        "ci": [p.statistics.ci_low, p.statistics.ci_high],
                    }
                    for p in regime_result.path
                ],
                "integrity_review_events": [
                    {"session": e.session.isoformat(), "flags": list(e.integrity_flags)}
                    for e in events
                    if e.integrity_flags and e.status is EventStatus.RESOLVED
                ],
            }

            # Lineage: lagged share and the two ingestion regimes inside the split-half.
            lagged = sum(e.timing_class.value == "lagged" for e in resolved)
            early = [e for e in resolved if e.session < INGESTION_BREAK]
            late = [e for e in resolved if e.session >= INGESTION_BREAK]
            entry["lineage"] = {
                "lagged_resolved": lagged,
                "lagged_share": lagged / len(resolved) if resolved else None,
                "events_before_2026-08-17": len(early),
                "events_from_2026-08-17": len(late),
                "mean_before": float(np.mean([e.primary_market_adjusted_return for e in early]))
                if early
                else None,
                "mean_from": float(np.mean([e.primary_market_adjusted_return for e in late]))
                if late
                else None,
                "chronological_split_straddles_ingestion_break": bool(
                    resolved
                    and resolved[len(resolved) // 2 - 1].session < INGESTION_BREAK
                    and resolved[len(resolved) // 2].session >= INGESTION_BREAK
                )
                if len(resolved) >= 2
                else None,
                "median_articles_per_event": float(np.median([e.article_count for e in resolved]))
                if resolved
                else None,
            }

            # Relevance noise proxy: titles that name neither the ticker nor the company.
            def noise(article_ids, aliases=ALIASES[ticker]) -> tuple[int, int, int]:
                total = off = junk = 0
                for fingerprint in article_ids:
                    row = connection.execute(
                        "SELECT title FROM articles WHERE fingerprint = ?", (fingerprint,)
                    ).fetchone()
                    total += 1
                    off += not any(a in row[0].casefold() for a in aliases)
                    junk += bool(_JUNK_TITLE.search(row[0]))
                return off, junk, total

            event_ids = [i for e in resolved for i in e.article_ids]
            all_ids = [i for s in signals for i in s.article_ids]
            off_e, junk_e, tot_e = noise(event_ids)
            off_a, junk_a, tot_a = noise(all_ids)
            entry["relevance_noise_proxy"] = {
                "definition": "kept articles with no ticker/company name in the title, and "
                "kept articles whose title matches a quote-page/aggregator pattern",
                "columns": ["no_company_name", "quote_page_pattern", "articles"],
                "event_sessions": [off_e, junk_e, tot_e],
                "all_signal_sessions": [off_a, junk_a, tot_a],
            }

            # Issuer-controlled channels: sources whose name carries the company's own name. They
            # cannot corroborate the company's own news (CLAUDE.md invariant 5). The engine already
            # collapses them to one `official-company` voice, but that voice still counts toward 3.
            def issuer(source: str, aliases=ALIASES[ticker]) -> bool:
                return source == "official-company" or any(a in source.casefold() for a in aliases)

            entry["issuer_channel_effect"] = {
                "events_with_any_issuer_source": sum(
                    any(issuer(x) for x in e.sources) for e in resolved
                ),
                "events_below_3_sources_if_issuer_sources_removed": sum(
                    sum(not issuer(x) for x in e.sources) < 3 for e in resolved
                ),
                "resolved_events": len(resolved),
            }

            # Lag shift: anchor every signal one session later / earlier, rerun the procedure.
            shifts = {}
            for shift in (1, -1):
                moved = []
                for s in signals:
                    index = calendar.index_of(s.session) + shift
                    if 0 <= index < len(calendar.sessions):
                        moved.append(s.model_copy(update={"session": calendar.sessions[index]}))
                shifted_events = _build_events(moved, calendar, stock, bench, thresholds, {})
                values = _resolved_values(shifted_events, regime)
                state, info = _state_of(values, bootstrap_seed(regime.value, f"lag{shift}"))
                summary = (
                    summarize_returns(values, bootstrap_seed(regime.value, f"lag{shift}"))
                    if values
                    else None
                )
                shifts[f"{shift:+d}"] = {
                    "state": state,
                    "n": len(values),
                    "mean": summary.mean if summary else None,
                    "median": summary.median if summary else None,
                    "ci": [summary.ci_low, summary.ci_high] if summary else None,
                }
            entry["lag_shift"] = shifts

            # Signal permutation: break sentiment <-> return, keep marginals, rerun everything.
            eligible = [k for k, s in enumerate(signals) if s.distinct_source_count >= 3]
            generator = np.random.default_rng(
                int.from_bytes(
                    bootstrap_seed("perm", ticker, regime.value).to_bytes(8, "big")[:4], "big"
                )
            )
            states: Counter = Counter()
            passes = 0
            sizes = []
            for _ in range(PLACEBO_DRAWS):
                shuffled = generator.permutation([signals[k].signal for k in eligible])
                permuted = list(signals)
                for slot, value in zip(eligible, shuffled, strict=True):
                    permuted[slot] = signals[slot].model_copy(update={"signal": float(value)})
                values = _resolved_values(
                    _build_events(permuted, calendar, stock, bench, thresholds, {}), regime
                )
                state, info = _state_of(values, bootstrap_seed(regime.value, "perm"))
                states[state] += 1
                passes += bool(info.get("passes_ci_and_effect"))
                sizes.append(len(values))
            entry["signal_permutation"] = {
                "draws": PLACEBO_DRAWS,
                "state_rates": {k: v / PLACEBO_DRAWS for k, v in sorted(states.items())},
                "share_with_n_at_least_20": float(np.mean([n >= 20 for n in sizes])),
                "share_passing_ci_and_effect_given_n20": passes / PLACEBO_DRAWS,
                "median_n": float(np.median(sizes)),
            }

            # Fixed-n random-session placebo: n = 20 non-overlapping sessions drawn from the span.
            fixed: Counter = Counter()
            fixed_passes = 0
            positions = np.asarray(in_span)
            for draw in range(PLACEBO_DRAWS):
                order = generator.permutation(positions)
                chosen = _greedy_spaced(order, MIN_EVENTS_VERDICT, PRIMARY_HORIZON)
                if len(chosen) < MIN_EVENTS_VERDICT:
                    continue
                state, info = _state_of(
                    [universe[i] for i in chosen], bootstrap_seed(regime.value, "fixed", str(draw))
                )
                fixed[state] += 1
                fixed_passes += bool(info.get("passes_ci_and_effect"))
            total = sum(fixed.values())
            entry["random_session_placebo_n20"] = {
                "draws": total,
                "state_rates": {k: v / total for k, v in sorted(fixed.items())},
                "share_passing_ci_and_effect": fixed_passes / total,
            }

            # Face validity: a deterministic sample traced to article IDs.
            sample_generator = np.random.default_rng(
                int.from_bytes(
                    bootstrap_seed("face", ticker, regime.value).to_bytes(8, "big")[:4], "big"
                )
            )
            picks = sample_generator.permutation(len(resolved))[:6] if resolved else []
            sample = []
            for pick in sorted(int(p) for p in picks):
                event = resolved[pick]
                rows = []
                for fingerprint in event.article_ids[:5]:
                    row = connection.execute(
                        "SELECT title, source, published_at FROM articles WHERE fingerprint = ?",
                        (fingerprint,),
                    ).fetchone()
                    rows.append({"article_id": fingerprint[:12], "title": row[0], "source": row[1]})
                sample.append(
                    {
                        "session": event.session.isoformat(),
                        "signal": round(event.signal, 3),
                        "timing": event.timing_class.value,
                        "articles": event.article_count,
                        "sources": event.distinct_source_count,
                        "stock_h0": event.stock_returns[0],
                        "market_adjusted_h0": event.market_adjusted_returns[0],
                        "market_adjusted_h5": event.primary_market_adjusted_return,
                        "headlines": rows,
                    }
                )
            entry["face_validity_sample"] = sample
            company["regimes"][regime.value] = entry
        output["companies"][ticker] = company

    if wanted:
        output["synthetic_null"] = synthetic_null(float(np.mean(sigma_pool)))

    # Calendar/benchmark alignment only. This does NOT validate the London path: no London-listed
    # company has stored articles.
    london = build_session_calendar(ListingExchange.LONDON, date(2023, 10, 1), date(2026, 10, 7))
    cukx = {date.fromisoformat(i["date"]) for i in payload["prices"]["CUKX.L"]}
    output["cukx_calendar_alignment_only"] = {
        "cukx_dates": len(cukx),
        "xlon_sessions_in_range": sum(
            date(2023, 10, 6) <= d <= date(2026, 10, 6) for d in london.sessions
        ),
        "cukx_not_on_xlon": sorted(d.isoformat() for d in cukx - set(london.sessions))[:5],
        "note": "calendar alignment only; the London path is untested in validation",
    }
    connection.close()
    args.output.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
