"""Deterministic event-study statistics and the `mr-v1` evidence/verdict rules."""

import hashlib
import math
from collections.abc import Sequence
from statistics import NormalDist

import numpy as np

from marketsentinel.market_reaction.models import (
    BOOTSTRAP_CONFIDENCE,
    BOOTSTRAP_RESAMPLES,
    DEFAULT_INTERVAL_METHOD,
    METHODOLOGY_VERSION,
    MIN_ABS_MEAN_RETURN,
    MIN_EVENTS_PRELIMINARY,
    MIN_EVENTS_VERDICT,
    SPEARMAN_MIN_SESSIONS,
    EvidenceState,
    IntervalMethod,
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


def mean_interval(
    values: Sequence[float],
    seed: int,
    method: IntervalMethod = DEFAULT_INTERVAL_METHOD,
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = BOOTSTRAP_CONFIDENCE,
) -> tuple[float, float, bool]:
    """Two-sided interval for the mean as `(low, high, degenerate)`.

    Every method returns the degenerate result when the sample has fewer than two observations,
    non-finite values or no spread, or when the computed bounds are non-finite or equal: a finite
    interval between zero and the mean, which contains zero, never a `nan` and never a spurious
    "excludes zero". `ci_excludes_zero` enforces the same rule a second time at the verdict.
    """

    data = np.asarray(values, dtype=float)
    if data.size == 0:
        raise ValueError("interval requires at least one observation")
    mean = float(data.mean())
    degenerate = (min(mean, 0.0), max(mean, 0.0), True)
    if data.size < 2 or not np.all(np.isfinite(data)) or float(data.max()) == float(data.min()):
        return degenerate
    bounds = _interval_bounds(data, seed, method, resamples, confidence)
    if bounds is None:
        return degenerate
    low, high = bounds
    if not (math.isfinite(low) and math.isfinite(high)) or low == high:
        return degenerate
    return low, high, False


def _interval_bounds(
    data: np.ndarray, seed: int, method: IntervalMethod, resamples: int, confidence: float
) -> tuple[float, float] | None:
    mean = float(data.mean())
    tail = (1 - confidence) / 2
    if method is IntervalMethod.PERCENTILE_BOOTSTRAP:
        return bootstrap_mean_ci(data, seed, resamples, confidence)
    if method is IntervalMethod.STUDENT_T:
        half = _student_t_quantile(1 - tail, data.size - 1) * _standard_error(data)
        return mean - half, mean + half
    generator = np.random.Generator(np.random.PCG64(seed))
    indices = generator.integers(0, data.size, size=(resamples, data.size))
    draws = data[indices]
    means = draws.mean(axis=1)
    if method is IntervalMethod.BOOTSTRAP_T:
        spread = draws.std(axis=1, ddof=1)
        usable = spread > 0
        if not usable.any():
            return None
        studentized = (means[usable] - mean) / (spread[usable] / math.sqrt(data.size))
        low_q, high_q = np.quantile(studentized, [tail, 1 - tail])
        error = _standard_error(data)
        return float(mean - high_q * error), float(mean - low_q * error)
    if method is IntervalMethod.BCA:
        return _bca_bounds(data, means, mean, tail)
    raise ValueError(f"unknown interval method: {method}")


def _standard_error(data: np.ndarray) -> float:
    return float(data.std(ddof=1)) / math.sqrt(data.size)


def _bca_bounds(
    data: np.ndarray, means: np.ndarray, mean: float, tail: float
) -> tuple[float, float]:
    """Bias-corrected and accelerated bounds (Efron 1987); acceleration from the jackknife."""

    normal = NormalDist()
    resamples = means.size
    below = (float((means < mean).sum()) + 0.5 * float((means == mean).sum())) / resamples
    below = min(max(below, 0.5 / resamples), 1 - 0.5 / resamples)
    bias = normal.inv_cdf(below)
    leave_one_out = (data.sum() - data) / (data.size - 1)
    centred = leave_one_out.mean() - leave_one_out
    denominator = 6 * float((centred**2).sum()) ** 1.5
    acceleration = float((centred**3).sum()) / denominator if denominator > 0 else 0.0
    levels = []
    for z in (normal.inv_cdf(tail), normal.inv_cdf(1 - tail)):
        shift = bias + z
        scale = 1 - acceleration * shift
        # A non-positive scale only occurs for extreme acceleration; clamp to the nearest tail.
        adjusted = normal.cdf(bias + shift / scale) if scale > 0 else (1.0 if shift > 0 else 0.0)
        levels.append(min(max(adjusted, 0.0), 1.0))
    low, high = np.quantile(means, levels)
    return float(low), float(high)


def _student_t_quantile(probability: float, degrees: int) -> float:
    """Quantile of Student's t for `probability` in (0.5, 1), by bisection on the exact CDF."""

    low, high = 0.0, 1.0
    while _student_t_cdf(high, degrees) < probability:
        high *= 2
    for _ in range(100):
        middle = (low + high) / 2
        if _student_t_cdf(middle, degrees) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _student_t_cdf(t: float, degrees: int) -> float:
    """CDF for t >= 0 through the regularized incomplete beta function."""

    x = degrees / (degrees + t * t)
    return 1 - 0.5 * _regularized_beta(x, degrees / 2, 0.5)


def _regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1) / (a + b + 2):
        return front * _beta_fraction(x, a, b) / a
    return 1 - front * _beta_fraction(1 - x, b, a) / b


def _beta_fraction(x: float, a: float, b: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""

    tiny = 1e-300
    c, d = 1.0, 1 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    result = d
    for m in range(1, 400):
        for numerator in (
            m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m)),
            -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1)),
        ):
            d = 1 + numerator * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + numerator / c
            c = c if abs(c) > tiny else tiny
            result *= d * c
        if abs(d * c - 1) < 1e-15:
            break
    return result


def summarize_returns(
    values: Sequence[float],
    seed: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
    interval_method: IntervalMethod = DEFAULT_INTERVAL_METHOD,
) -> ReturnStatistics:
    data = np.asarray(values, dtype=float)
    low, high, degenerate = mean_interval(values, seed, interval_method, resamples)
    return ReturnStatistics(
        n=int(data.size),
        mean=float(data.mean()),
        median=float(np.median(data)),
        ci_low=low,
        ci_high=high,
        share_positive=float((data > 0).mean()),
        interval_method=interval_method,
        interval_degenerate=degenerate,
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
    """False for a degenerate or zero-width interval, whatever its bounds are (second guard)."""

    if statistics.interval_degenerate or statistics.ci_low == statistics.ci_high:
        return False
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
