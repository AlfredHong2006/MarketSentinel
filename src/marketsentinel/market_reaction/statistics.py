"""Deterministic event-study statistics and the `mr-v1` evidence/verdict rules."""

import hashlib
import math
from collections.abc import Sequence

import numpy as np

from marketsentinel.market_reaction.models import (
    BOOTSTRAP_CONFIDENCE,
    BOOTSTRAP_RESAMPLES,
    METHODOLOGY_VERSION,
    MIN_ABS_MEAN_RETURN,
    MIN_EVENTS_PRELIMINARY,
    MIN_EVENTS_VERDICT,
    SPEARMAN_MIN_SESSIONS,
    EvidenceState,
    ReturnStatistics,
    SpearmanResult,
)


def bootstrap_seed(*scope: str, methodology_version: str = METHODOLOGY_VERSION) -> int:
    """Fixed seed derived from the methodology version plus a stable scope label.

    The scope (regime, horizon) keeps separate intervals from sharing one resample stream while
    every seed remains a pure function of the methodology version.
    """

    label = "|".join((methodology_version, "bootstrap", *scope))
    return int.from_bytes(hashlib.sha256(label.encode("utf-8")).digest()[:8], "big")


def bootstrap_mean_ci(
    values: Sequence[float],
    seed: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = BOOTSTRAP_CONFIDENCE,
) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean (iid resampling of events with replacement)."""

    data = np.asarray(values, dtype=float)
    if data.size == 0:
        raise ValueError("bootstrap requires at least one observation")
    generator = np.random.Generator(np.random.PCG64(seed))
    indices = generator.integers(0, data.size, size=(resamples, data.size))
    means = data[indices].mean(axis=1)
    tail = (1 - confidence) / 2
    low, high = np.quantile(means, [tail, 1 - tail])
    return float(low), float(high)


def summarize_returns(
    values: Sequence[float], seed: int, resamples: int = BOOTSTRAP_RESAMPLES
) -> ReturnStatistics:
    data = np.asarray(values, dtype=float)
    low, high = bootstrap_mean_ci(values, seed, resamples)
    return ReturnStatistics(
        n=int(data.size),
        mean=float(data.mean()),
        median=float(np.median(data)),
        ci_low=low,
        ci_high=high,
        share_positive=float((data > 0).mean()),
    )


def split_half_means(chronological_values: Sequence[float]) -> tuple[float, float]:
    """Means of the chronological first half (floor(n/2) events) and the remainder."""

    data = np.asarray(chronological_values, dtype=float)
    if data.size < 2:
        raise ValueError("split-half needs at least two observations")
    middle = data.size // 2
    return float(data[:middle].mean()), float(data[middle:].mean())


def same_sign(first: float, second: float) -> bool:
    """Strict agreement: a zero on either side is not agreement."""

    return (first > 0 and second > 0) or (first < 0 and second < 0)


def regime_state(
    statistics: ReturnStatistics | None,
    first_half_mean: float | None,
    second_half_mean: float | None,
) -> EvidenceState:
    """Apply spec section 11 to one regime's resolved +5 market-adjusted returns."""

    n = statistics.n if statistics is not None else 0
    if statistics is None or n < MIN_EVENTS_PRELIMINARY:
        return EvidenceState.INSUFFICIENT_EVENTS
    if n < MIN_EVENTS_VERDICT:
        return EvidenceState.PRELIMINARY
    if not (ci_excludes_zero(statistics) and meets_minimum_effect(statistics)):
        return EvidenceState.NO_CONSISTENT_RELATIONSHIP
    stable = (
        first_half_mean is not None
        and second_half_mean is not None
        and same_sign(first_half_mean, second_half_mean)
    )
    if stable and same_sign(statistics.mean, statistics.median):
        return EvidenceState.DETECTED
    return EvidenceState.UNSTABLE


def ci_excludes_zero(statistics: ReturnStatistics) -> bool:
    return statistics.ci_low > 0 or statistics.ci_high < 0


def meets_minimum_effect(statistics: ReturnStatistics) -> bool:
    return abs(statistics.mean) >= MIN_ABS_MEAN_RETURN


def spearman(
    signals: Sequence[float], returns: Sequence[float], minimum: int = SPEARMAN_MIN_SESSIONS
) -> SpearmanResult | None:
    """Rank correlation with average ranks for ties; None below the minimum or when degenerate."""

    if len(signals) != len(returns):
        raise ValueError("signals and returns must align")
    if len(signals) < minimum:
        return None
    first = _average_ranks(np.asarray(signals, dtype=float))
    second = _average_ranks(np.asarray(returns, dtype=float))
    if first.std() == 0 or second.std() == 0:
        return None
    rho = float(np.corrcoef(first, second)[0, 1])
    if not math.isfinite(rho):
        return None
    return SpearmanResult(n=len(signals), rho=max(-1.0, min(1.0, rho)))


def _average_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="stable")
    ranks = np.empty(values.size, dtype=float)
    ordered = values[order]
    start = 0
    while start < values.size:
        stop = start
        while stop + 1 < values.size and ordered[stop + 1] == ordered[start]:
            stop += 1
        ranks[order[start : stop + 1]] = (start + stop) / 2 + 1
        start = stop + 1
    return ranks
