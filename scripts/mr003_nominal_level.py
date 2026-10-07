"""MR-003 follow-up: decompose the verdict rule's false-detection rate on mean-zero noise.

Adds each of the spec section 11 conditions in turn, so the contribution of the 95% interval alone
(the only condition with a stated nominal level) can be read against the full rule. Pure numpy; no
database, no prices, no network. The scale is the mean unconditional NVDA/PFE +5 market-adjusted
standard deviation reported by `scripts/mr003_validate.py`.
"""

from __future__ import annotations

import json

import numpy as np

from marketsentinel.market_reaction.statistics import (
    bootstrap_seed,
    ci_excludes_zero,
    meets_minimum_effect,
    same_sign,
    split_half_means,
    summarize_returns,
)

SIGMA = 0.04311613440889969
TRIALS = 4000


def main() -> None:
    generator = np.random.default_rng(20261006)
    out: dict = {"sigma": SIGMA, "trials": TRIALS}
    for label, draw in (
        ("normal", lambda n: generator.normal(0, SIGMA, n)),
        ("student_t3", lambda n: SIGMA / np.sqrt(3) * generator.standard_t(3, n)),
    ):
        for n in (20, 30, 50):
            counts = dict.fromkeys(("ci", "ci_effect", "ci_effect_split", "full"), 0)
            for trial in range(TRIALS):
                values = draw(n)
                stats = summarize_returns(list(values), bootstrap_seed("nominal", str(trial)))
                first, second = split_half_means(list(values))
                ci = ci_excludes_zero(stats)
                effect = ci and meets_minimum_effect(stats)
                split = effect and same_sign(first, second)
                counts["ci"] += ci
                counts["ci_effect"] += effect
                counts["ci_effect_split"] += split
                counts["full"] += split and same_sign(stats.mean, stats.median)
            out[f"{label}/n={n}"] = {k: v / TRIALS for k, v in counts.items()}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
