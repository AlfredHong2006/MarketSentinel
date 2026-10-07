"""MR-008: false-detection level and power of candidate interval methods, on synthetic returns.

Synthetic data only: every sample is drawn here from a distribution with a known true mean. No
database, no price, no fixture, no network. Every simulation is seeded from the methodology
seed function, so a rerun with the same arguments is byte-identical.

Modes (one script, all behind the first argument):

  reproduce  the current percentile bootstrap on MR-003's two mean-zero shapes (n = 20, 30, 50)
  grid       every candidate on the full grid, identical samples per cell, written to --out
  timing     wall time of one interval per method at the default resample count
  select     apply the selection rule below to a grid file

Selection rule, fixed before any result (packet MR-008, section 3):

  1. On every mean-zero shape at every n in {20, 25, 30, 40, 50} the interval-alone rejection rate
     is at most 0.05 + 2 * sqrt(0.05 * 0.95 / trials).
  2. Among candidates passing 1, prefer the highest power: the mean interval-alone rejection rate
     over the four shapes and the four true means (+-1%, +-2%), averaged over n = 20 and n = 30.
  3. Cost: at most about one second per interval at the default resample count (n up to 100).
  4. Ties (power within one Monte-Carlo standard error of the best) go to the simpler method,
     in the order student_t, percentile_bootstrap, bootstrap_t, bca.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from marketsentinel.market_reaction.models import (
    BOOTSTRAP_RESAMPLES,
    EvidenceState,
    IntervalMethod,
)
from marketsentinel.market_reaction.statistics import (
    bootstrap_seed,
    ci_excludes_zero,
    meets_minimum_effect,
    regime_state,
    same_sign,
    split_half_means,
    summarize_returns,
)

SIGMA = 0.04311613440889969  # MR-003's mean unconditional +5 market-adjusted standard deviation
NS = (10, 15, 20, 25, 30, 40, 50, 75, 100)
SELECTION_NS = (20, 25, 30, 40, 50)
MEANS = (0.0, 0.01, -0.01, 0.02, -0.02)
SHAPES = ("normal", "student_t3", "skewed_exponential", "contaminated")
METHODS = tuple(IntervalMethod)
SIMPLICITY_ORDER = (
    IntervalMethod.STUDENT_T,
    IntervalMethod.PERCENTILE_BOOTSTRAP,
    IntervalMethod.BOOTSTRAP_T,
    IntervalMethod.BCA,
)
NULL_TRIALS = 8000  # SE at a 5% rate: sqrt(.05 * .95 / 8000) = 0.244 pp
POWER_TRIALS = 8000
MASTER_SEED = 20261007


def draw(shape: str, generator: np.random.Generator, trials: int, n: int) -> np.ndarray:
    """Mean-zero samples, each with standard deviation SIGMA."""

    if shape == "normal":
        return generator.normal(0.0, SIGMA, (trials, n))
    if shape == "student_t3":
        return SIGMA / math.sqrt(3) * generator.standard_t(3, (trials, n))
    if shape == "skewed_exponential":  # skewness 2, bounded below
        return SIGMA * (generator.exponential(1.0, (trials, n)) - 1.0)
    if shape == "contaminated":  # 90% small moves, 10% moves five times larger
        small = SIGMA / math.sqrt(0.9 + 0.1 * 25)
        scale = np.where(generator.random((trials, n)) < 0.1, 5 * small, small)
        return generator.normal(0.0, 1.0, (trials, n)) * scale
    raise ValueError(shape)


def cell_generator(shape: str, n: int, mean: float) -> np.random.Generator:
    seed = bootstrap_seed("mr008", "cell", shape, str(n), f"{mean:+.2f}", str(MASTER_SEED))
    return np.random.Generator(np.random.PCG64(seed))


def run_cell(task: tuple[str, int, float, int]) -> dict:
    shape, n, mean, trials = task
    samples = draw(shape, cell_generator(shape, n, mean), trials, n) + mean
    counts = {m.value: [0, 0, 0.0] for m in METHODS}  # interval alone, full rule, width sum
    for trial in range(trials):
        values = samples[trial]
        seed = bootstrap_seed("mr008", "boot", shape, str(n), str(trial))
        first, second = split_half_means(values)
        for method in METHODS:
            stats = summarize_returns(values, seed, interval_method=method)
            excludes = ci_excludes_zero(stats)
            full = (
                excludes
                and meets_minimum_effect(stats)
                and same_sign(first, second)
                and same_sign(stats.mean, stats.median)
            )
            row = counts[method.value]
            row[0] += excludes
            row[1] += full
            row[2] += stats.ci_high - stats.ci_low
    return {
        "shape": shape,
        "n": n,
        "mean": mean,
        "trials": trials,
        "methods": {
            name: {"interval_rate": a / trials, "full_rate": f / trials, "width": w / trials}
            for name, (a, f, w) in counts.items()
        },
    }


def standard_error(rate: float, trials: int) -> float:
    return math.sqrt(rate * (1 - rate) / trials)


def run_grid(workers: int, null_trials: int, power_trials: int, ns: tuple[int, ...]) -> list[dict]:
    tasks = [
        (shape, n, mean, null_trials if mean == 0.0 else power_trials)
        for shape in SHAPES
        for n in ns
        for mean in MEANS
    ]
    tasks.sort(key=lambda t: -t[1] * t[3])  # longest first
    with Pool(workers) as pool:
        cells = list(pool.imap_unordered(run_cell, tasks))
    cells.sort(key=lambda c: (SHAPES.index(c["shape"]), c["n"], c["mean"]))
    return cells


def sanity_check_full_rule() -> None:
    """The script's inlined full rule must equal the engine's `regime_state` at n >= 20."""

    generator = np.random.default_rng(1)
    for trial in range(300):
        values = generator.normal(0.01, SIGMA, 25)
        stats = summarize_returns(values, trial)
        first, second = split_half_means(values)
        engine = regime_state(stats, first, second) is EvidenceState.DETECTED
        inline = (
            ci_excludes_zero(stats)
            and meets_minimum_effect(stats)
            and same_sign(first, second)
            and same_sign(stats.mean, stats.median)
        )
        assert engine == inline


def mode_reproduce(args: argparse.Namespace) -> None:
    sanity_check_full_rule()
    cells = run_grid(args.workers, args.null_trials, args.power_trials, (20, 30, 50))
    print("MR-003 reproduction: percentile bootstrap on mean-zero data, sigma = 4.31%")
    print(
        f"trials {args.null_trials}; SE at 5% = {100 * standard_error(0.05, args.null_trials):.2f} pp"
    )
    for cell in cells:
        if cell["mean"] != 0.0 or cell["shape"] not in ("normal", "student_t3"):
            continue
        row = cell["methods"]["percentile_bootstrap"]
        se_a = 100 * standard_error(row["interval_rate"], cell["trials"])
        se_f = 100 * standard_error(row["full_rate"], cell["trials"])
        print(
            f"{cell['shape']:<12} n={cell['n']:<3} interval alone "
            f"{100 * row['interval_rate']:.2f}% (SE {se_a:.2f}) | full rule "
            f"{100 * row['full_rate']:.2f}% (SE {se_f:.2f})"
        )


def mode_grid(args: argparse.Namespace) -> None:
    sanity_check_full_rule()
    started = time.perf_counter()
    cells = run_grid(args.workers, args.null_trials, args.power_trials, NS)
    payload = {
        "sigma": SIGMA,
        "null_trials": args.null_trials,
        "power_trials": args.power_trials,
        "resamples": BOOTSTRAP_RESAMPLES,
        "master_seed": MASTER_SEED,
        "cells": cells,
    }
    Path(args.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"wrote {len(cells)} cells to {args.out} in {time.perf_counter() - started:.0f}s")


def mode_timing(_: argparse.Namespace) -> None:
    generator = np.random.default_rng(5)
    print("seconds per interval at the default resample count (best of 5)")
    for n in (20, 50, 100):
        values = generator.normal(0, SIGMA, n)
        for method in METHODS:
            best = min(_time_once(values, method) for _ in range(5))
            print(f"n={n:<3} {method.value:<22} {best:.4f}s")


def _time_once(values: np.ndarray, method: IntervalMethod) -> float:
    started = time.perf_counter()
    summarize_returns(values, bootstrap_seed("mr008", "timing"), interval_method=method)
    return time.perf_counter() - started


def mode_select(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.grid).read_text(encoding="utf-8"))
    cells = payload["cells"]
    trials = payload["null_trials"]
    limit = 0.05 + 2 * standard_error(0.05, trials)
    print(f"condition 1 limit: {100 * limit:.2f}% (5% + 2 SE, {trials} trials)")
    passing = []
    for method in METHODS:
        name = method.value
        null = [c for c in cells if c["mean"] == 0.0 and c["n"] in SELECTION_NS]
        worst = max(null, key=lambda c: c["methods"][name]["interval_rate"])
        worst_rate = worst["methods"][name]["interval_rate"]
        failures = [c for c in null if c["methods"][name]["interval_rate"] > limit]
        power = {}
        for n in (20, 30):
            rates = [
                c["methods"][name]["interval_rate"]
                for c in cells
                if c["n"] == n and c["mean"] != 0.0
            ]
            power[n] = sum(rates) / len(rates)
        score = (power[20] + power[30]) / 2
        verdict = "PASS" if not failures else f"FAIL ({len(failures)} of {len(null)} cells)"
        print(
            f"{name:<22} worst level {100 * worst_rate:.2f}% "
            f"({worst['shape']}, n={worst['n']}) {verdict}; power n20 {100 * power[20]:.1f}% "
            f"n30 {100 * power[30]:.1f}% mean {100 * score:.1f}%"
        )
        if not failures:
            passing.append((method, score))
    if not passing:
        print("RESULT: no candidate satisfies condition 1; nothing qualified.")
        return
    best = max(score for _, score in passing)
    power_se = standard_error(best, payload["power_trials"] * 16 * 2)
    tied = [m for m, score in passing if best - score <= power_se]
    chosen = min(tied, key=SIMPLICITY_ORDER.index)
    print(f"RESULT: qualifying {[m.value for m, _ in passing]}; recommended {chosen.value}")
    print(f"(power tie band {100 * power_se:.2f} pp; tied {[m.value for m in tied]})")


def mode_tables(args: argparse.Namespace) -> None:
    payload = json.loads(Path(args.grid).read_text(encoding="utf-8"))
    cells = {(c["shape"], c["n"], c["mean"]): c for c in payload["cells"]}
    names = [m.value for m in METHODS]
    for shape in SHAPES:
        print(f"\n### {shape}: true mean 0, interval-alone / full-rule % (width %)")
        print("| n | " + " | ".join(names) + " |")
        print("|---|" + "---|" * len(names))
        for n in NS:
            row = cells[(shape, n, 0.0)]["methods"]
            print(
                f"| {n} | "
                + " | ".join(
                    f"{100 * row[k]['interval_rate']:.1f} / {100 * row[k]['full_rate']:.1f}"
                    f" ({100 * row[k]['width']:.1f})"
                    for k in names
                )
                + " |"
            )
        print(f"\n### {shape}: power, interval-alone / full-rule %, +- averaged")
        for mu in (0.01, 0.02):
            print(f"\nTrue mean magnitude {mu:.0%} (average of + and -)")
            print("| n | " + " | ".join(names) + " |")
            print("|---|" + "---|" * len(names))
            for n in NS:
                parts = []
                for k in names:
                    pair = [cells[(shape, n, s * mu)]["methods"][k] for s in (1, -1)]
                    a = 100 * sum(x["interval_rate"] for x in pair) / 2
                    f = 100 * sum(x["full_rate"] for x in pair) / 2
                    parts.append(f"{a:.1f} / {f:.1f}")
                print(f"| {n} | " + " | ".join(parts) + " |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    for name, handler in (("reproduce", mode_reproduce), ("grid", mode_grid)):
        p = sub.add_parser(name)
        p.add_argument("--workers", type=int, default=8)
        p.add_argument("--null-trials", type=int, default=NULL_TRIALS)
        p.add_argument("--power-trials", type=int, default=POWER_TRIALS)
        if name == "grid":
            p.add_argument("--out", required=True)
        p.set_defaults(handler=handler)
    sub.add_parser("timing").set_defaults(handler=mode_timing)
    p = sub.add_parser("tables")
    p.add_argument("--grid", required=True)
    p.set_defaults(handler=mode_tables)
    p = sub.add_parser("select")
    p.add_argument("--grid", required=True)
    p.set_defaults(handler=mode_select)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
