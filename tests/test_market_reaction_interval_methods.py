"""Interval-method option for the mean (MR-008). Synthetic inputs only; no I/O."""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from marketsentinel.market_reaction import statistics as statistics_module
from marketsentinel.market_reaction.models import (
    DEFAULT_INTERVAL_METHOD,
    EvidenceState,
    IntervalMethod,
    ReturnStatistics,
)
from marketsentinel.market_reaction.statistics import (
    _student_t_quantile,
    bootstrap_mean_ci,
    bootstrap_seed,
    ci_excludes_zero,
    mean_interval,
    regime_state,
    summarize_returns,
)

SAMPLE = [0.01, -0.02, 0.03, 0.015, -0.005, 0.02, 0.04, -0.01, 0.0, 0.025]
CANDIDATES = (IntervalMethod.BOOTSTRAP_T, IntervalMethod.BCA, IntervalMethod.STUDENT_T)


def test_default_is_student_t_and_percentile_bootstrap_is_unchanged_when_selected():
    assert DEFAULT_INTERVAL_METHOD is IntervalMethod.STUDENT_T
    seed = bootstrap_seed("clearly_positive", "h5")
    default = summarize_returns(SAMPLE, seed)
    assert default == summarize_returns(SAMPLE, seed, interval_method=IntervalMethod.STUDENT_T)
    assert default.interval_method is IntervalMethod.STUDENT_T
    assert default.interval_degenerate is False
    percentile = summarize_returns(
        SAMPLE, seed, interval_method=IntervalMethod.PERCENTILE_BOOTSTRAP
    )
    assert (percentile.ci_low, percentile.ci_high) == bootstrap_mean_ci(SAMPLE, seed)


def test_student_t_quantiles_match_published_values():
    published = {1: 12.7062, 2: 4.3027, 9: 2.2622, 19: 2.0930, 29: 2.0452, 99: 1.9842}
    for degrees, expected in published.items():
        assert _student_t_quantile(0.975, degrees) == pytest.approx(expected, abs=5e-5)
    assert _student_t_quantile(0.995, 10) == pytest.approx(3.1693, abs=5e-5)


def test_student_t_interval_exact_on_a_hand_checkable_sample():
    # mean 0.03, s = 0.015811, se = 0.0070711, t(0.975, 4) = 2.7764 -> half-width 0.0196324
    stats = summarize_returns(
        [0.01, 0.02, 0.03, 0.04, 0.05], seed=1, interval_method=IntervalMethod.STUDENT_T
    )
    assert stats.ci_low == pytest.approx(0.0103676, abs=1e-6)
    assert stats.ci_high == pytest.approx(0.0496324, abs=1e-6)
    assert stats.interval_method is IntervalMethod.STUDENT_T
    assert stats.interval_degenerate is False


@pytest.mark.parametrize("method", CANDIDATES)
def test_candidates_are_deterministic_and_record_their_method(method):
    seed = bootstrap_seed("clearly_negative", "h5")
    first = summarize_returns(SAMPLE, seed, interval_method=method)
    assert first == summarize_returns(SAMPLE, seed, interval_method=method)
    assert first.interval_method is method
    assert first.ci_low < first.mean < first.ci_high
    assert math.isfinite(first.ci_low) and math.isfinite(first.ci_high)
    # The point estimates do not depend on the interval method.
    base = summarize_returns(SAMPLE, seed)
    assert (first.n, first.mean, first.median, first.share_positive) == (
        base.n,
        base.mean,
        base.median,
        base.share_positive,
    )


@pytest.mark.parametrize("method", (IntervalMethod.BOOTSTRAP_T, IntervalMethod.BCA))
def test_bootstrap_candidates_depend_on_the_seed_only(method):
    low_a = mean_interval(SAMPLE, 1, method)
    assert low_a == mean_interval(SAMPLE, 1, method)
    assert low_a != mean_interval(SAMPLE, 2, method)


def test_student_t_is_order_independent():
    shuffled = list(np.random.default_rng(3).permutation(SAMPLE))
    assert mean_interval(shuffled, 1, IntervalMethod.STUDENT_T) == pytest.approx(
        mean_interval(SAMPLE, 1, IntervalMethod.STUDENT_T)
    )


def test_candidates_roughly_agree_on_well_behaved_data():
    data = list(np.random.default_rng(11).normal(0.0, 0.04, 200))
    reference = mean_interval(data, 7, IntervalMethod.STUDENT_T)
    for method in (IntervalMethod.BOOTSTRAP_T, IntervalMethod.BCA):
        low, high, degenerate = mean_interval(data, 7, method)
        assert not degenerate
        assert low == pytest.approx(reference[0], abs=2e-3)
        assert high == pytest.approx(reference[1], abs=2e-3)


@pytest.mark.parametrize("method", CANDIDATES)
@pytest.mark.parametrize("values", ([0.02], [0.02] * 25, [-0.03] * 25, [0.0] * 25))
def test_degenerate_samples_are_safe_and_never_exclude_zero(method, values):
    stats = summarize_returns(values, seed=1, interval_method=method)
    assert stats.interval_degenerate is True
    assert stats.interval_method is method
    assert math.isfinite(stats.ci_low) and math.isfinite(stats.ci_high)
    assert stats.ci_low <= 0 <= stats.ci_high
    assert ci_excludes_zero(stats) is False


@pytest.mark.parametrize("method", IntervalMethod)
def test_empty_sample_is_rejected_for_every_method(method):
    with pytest.raises(ValueError):
        mean_interval([], 1, method)


def test_interval_method_is_a_required_field():
    with pytest.raises(ValidationError):
        ReturnStatistics.model_validate(
            {
                "n": 3,
                "mean": 0.0,
                "median": 0.0,
                "ci_low": -1.0,
                "ci_high": 1.0,
                "share_positive": 0.5,
            }
        )


IDENTICAL_SHAPES = {
    "single": [0.02],
    "identical_positive": [0.01] * 25,
    "identical_negative": [-0.03] * 25,
    "identical_zero": [0.0] * 25,
    "non_finite": [0.01] * 24 + [float("nan")],
}


@pytest.mark.parametrize("method", IntervalMethod)
@pytest.mark.parametrize("values", IDENTICAL_SHAPES.values(), ids=IDENTICAL_SHAPES.keys())
def test_no_method_reaches_detected_or_unstable_from_a_degenerate_sample(method, values):
    # 25 identical returns of 1% give `detected` on main with the percentile bootstrap.
    stats = summarize_returns(values, seed=1, interval_method=method)
    assert stats.interval_degenerate is True
    assert ci_excludes_zero(stats) is False
    first = float(np.mean(values[: len(values) // 2])) if len(values) > 1 else 0.0
    second = float(np.mean(values[len(values) // 2 :])) if len(values) > 1 else 0.0
    state = regime_state(stats, first, second)
    assert state not in (EvidenceState.DETECTED, EvidenceState.UNSTABLE)


def test_equal_bounds_from_a_bootstrap_method_are_degenerate(monkeypatch):
    monkeypatch.setattr(statistics_module, "bootstrap_mean_ci", lambda *a, **k: (0.02, 0.02))
    low, high, degenerate = mean_interval(SAMPLE, 1, IntervalMethod.PERCENTILE_BOOTSTRAP)
    assert degenerate is True
    assert low <= 0 <= high
    monkeypatch.setattr(statistics_module, "_bca_bounds", lambda *a, **k: (0.02, 0.02))
    assert mean_interval(SAMPLE, 1, IntervalMethod.BCA)[2] is True


def test_the_verdict_guard_holds_without_the_flag():
    zero_width = ReturnStatistics(
        n=25,
        mean=0.01,
        median=0.01,
        ci_low=0.01,
        ci_high=0.01,
        share_positive=1.0,
        interval_method=IntervalMethod.PERCENTILE_BOOTSTRAP,
    )
    flagged = zero_width.model_copy(
        update={"ci_low": 0.005, "ci_high": 0.015, "interval_degenerate": True}
    )
    assert ci_excludes_zero(zero_width) is False
    assert ci_excludes_zero(flagged) is False
    assert regime_state(zero_width, 0.01, 0.01) is EvidenceState.NO_CONSISTENT_RELATIONSHIP


def test_seeded_false_exclusion_rate_on_mean_zero_normal_data_at_n_20():
    """Pins the measured level (MR-008) on a fixed synthetic run.

    2000 mean-zero normal samples of n = 20, sigma = 4.31%. The pinned rates are from this exact
    seeded run; the tolerance is 1.5 pp, about three Monte-Carlo standard errors at 2000 trials.
    No candidate met the MR-008 selection rule, so this pins the baseline's known excess and the
    two candidates that hold the nominal level on light tails, not a recommendation.
    """

    samples = np.random.default_rng(20261007).normal(0.0, 0.0431, (2000, 20))
    pinned = {
        IntervalMethod.PERCENTILE_BOOTSTRAP: 0.073,
        IntervalMethod.STUDENT_T: 0.0515,
        IntervalMethod.BOOTSTRAP_T: 0.051,
    }
    for method, expected in pinned.items():
        rejected = sum(
            ci_excludes_zero(
                summarize_returns(row, bootstrap_seed("reg", str(i)), interval_method=method)
            )
            for i, row in enumerate(samples)
        )
        assert rejected / 2000 == pytest.approx(expected, abs=0.015)
