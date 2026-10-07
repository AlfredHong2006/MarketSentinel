# MR-008 Proposal: interval method for the mean

Status: **no candidate qualified under the selection rule.** Alfred then decided (second pass) to
adopt the classical Student-t interval as the `mr-v1` default, disclose the measured levels, and
forbid any "excludes zero" from a zero-width interval. Sections 4 and 6 describe that decision as
implemented; sections 1-3 are the original analysis and are unchanged. Everything below is
simulated; no real return, outcome or evidence state was loaded.

Method: `scripts/mr008_interval_levels.py` (modes `reproduce`, `grid`, `timing`, `select`, `tables`).
Samples are drawn from known mean-zero (or ±1%, ±2%) distributions with standard deviation 4.31%
(MR-003's value). All four candidates are run on the **identical** sample in every trial, B = 2000,
seeds derived from the methodology seed function. **8,000 trials per cell** (null and power), so the
Monte-Carlo SE at a 5% rate is **0.24 pp**; at 7% it is 0.28 pp. 180 cells (9 n × 4 shapes × 5 true
means), about 9 minutes on 12 workers.

Shapes (all mean 0, SD 4.31%): normal; Student-t(3); skewed = centred exponential (skewness 2);
contaminated = 90% N(0, s) and 10% N(0, 5s). Candidates: percentile bootstrap (baseline), bootstrap-t
(studentized by each resample's own SE, no nested bootstrap), BCa (jackknife acceleration), classical
Student-t. No further candidate was added.

## 1. Measured tables

### 1a. MR-003 reproduced (percentile bootstrap, 8,000 trials)

| shape | n | interval alone | full rule |
|---|---|---|---|
| normal | 20 | 7.80% (SE 0.30) | 7.62% (SE 0.30) |
| normal | 30 | 6.22% (SE 0.27) | 6.01% (SE 0.27) |
| normal | 50 | 5.66% (SE 0.26) | 5.55% (SE 0.26) |
| Student-t(3) | 20 | 7.91% (SE 0.30) | 7.59% (SE 0.30) |
| Student-t(3) | 30 | 7.10% (SE 0.29) | 6.90% (SE 0.28) |
| Student-t(3) | 50 | 6.45% (SE 0.27) | 6.24% (SE 0.27) |

MR-003 reported 7.0/6.6/5.5% (normal) and 8.1/7.2/6.8% (t3) for the interval alone with 4,000 trials
(SE ≈ 0.4 pp). The new figures agree within sampling error: the problem is real, about 1.2–1.6× the
nominal level at n = 20–30. "Full rule" here is conditions 2–5 of spec section 11 with the `n ≥ 20`
condition waived so that n < 20 can be shown; at n ≥ 20 it is checked equal to the engine's
`regime_state == detected`.

### 1b. False-detection level on mean-zero data

Interval alone / full rule, %, and mean interval width, %. Limit under selection condition 1:
5% + 2 SE = **5.49%**. The full grid (every n, all candidates, width) is reproducible with
`tables`; the selection range n = 20–50 is below, plus the n = 10 and 100 ends.

Interval-alone rejection rate (%):

| shape | n | percentile | bootstrap-t | BCa | Student-t |
|---|---|---|---|---|---|
| normal | 10 | 9.9 | 5.1 | 10.0 | 4.9 |
| normal | 20 | 7.8 | 5.2 | 7.7 | 5.3 |
| normal | 25 | 6.9 | 5.2 | 7.0 | 5.0 |
| normal | 30 | 6.2 | 4.9 | 6.3 | 4.9 |
| normal | 40 | 6.4 | 5.3 | 6.4 | 5.3 |
| normal | 50 | 5.7 | 4.9 | 5.8 | 4.9 |
| normal | 100 | 6.0 | 5.7 | 6.1 | 5.4 |
| Student-t(3) | 10 | 10.6 | 6.4 | 13.3 | 3.6 |
| Student-t(3) | 20 | 7.9 | 7.3 | 10.9 | 4.2 |
| Student-t(3) | 25 | 8.0 | 8.1 | 10.8 | 4.5 |
| Student-t(3) | 30 | 7.1 | 7.4 | 10.0 | 4.4 |
| Student-t(3) | 40 | 7.3 | 8.0 | 9.8 | 4.6 |
| Student-t(3) | 50 | 6.5 | 7.4 | 9.1 | 4.5 |
| Student-t(3) | 100 | 5.9 | 6.8 | 7.7 | 4.5 |
| skewed | 10 | 13.7 | 5.5 | 12.2 | 9.8 |
| skewed | 20 | 9.5 | 5.3 | 8.3 | 8.0 |
| skewed | 25 | 9.0 | 5.3 | 8.0 | 8.0 |
| skewed | 30 | 7.8 | 5.0 | 7.3 | 6.9 |
| skewed | 40 | 7.8 | 5.1 | 7.0 | 6.8 |
| skewed | 50 | 6.9 | 5.0 | 6.2 | 6.4 |
| skewed | 100 | 6.0 | 5.0 | 5.9 | 5.9 |
| contaminated | 10 | 11.9 | 7.5 | 15.9 | 3.7 |
| contaminated | 20 | 8.4 | 8.8 | 13.5 | 3.6 |
| contaminated | 25 | 8.1 | 9.1 | 13.2 | 3.8 |
| contaminated | 30 | 7.9 | 9.4 | 12.6 | 3.9 |
| contaminated | 40 | 7.4 | 9.1 | 11.9 | 3.9 |
| contaminated | 50 | 7.7 | 9.7 | 11.7 | 4.2 |
| contaminated | 100 | 6.1 | 7.6 | 8.6 | 4.4 |

Full-rule `detected` rate (%) at the three headline sizes:

| shape | n | percentile | bootstrap-t | BCa | Student-t |
|---|---|---|---|---|---|
| normal | 20 / 30 / 50 | 7.6 / 6.0 / 5.5 | 5.2 / 4.7 / 4.8 | 7.6 / 6.0 / 5.7 | 5.2 / 4.7 / 4.8 |
| Student-t(3) | 20 / 30 / 50 | 7.6 / 6.9 / 6.2 | 7.0 / 7.1 / 7.1 | 10.2 / 9.3 / 8.4 | 4.2 / 4.4 / 4.4 |
| skewed | 20 / 30 / 50 | 9.4 / 7.5 / 6.0 | 5.1 / 4.7 / 3.9 | 7.9 / 6.4 / 4.7 | 7.9 / 6.8 / 6.0 |
| contaminated | 20 / 30 / 50 | 8.1 / 7.6 / 7.2 | 8.4 / 8.9 / 8.9 | 12.2 / 11.4 / 10.5 | 3.6 / 3.8 / 4.1 |

The other conditions remove almost nothing on symmetric shapes (≤ 0.3 pp), as MR-003 found. On the
skewed shape they remove more at larger n (mean/median sign disagreement), but that is the rule
doing something other than fixing the interval.

Mean interval width (%) at n = 20: normal 3.6 / 4.0 / 3.6 / 4.0 (percentile / bootstrap-t / BCa /
Student-t); the intervals the baseline reports are about 10% too narrow, which is the miscalibration.

### 1c. Power (interval alone / full rule, %; ± averaged), selected n

| shape | true mean | n | percentile | bootstrap-t | BCa | Student-t |
|---|---|---|---|---|---|---|
| normal | 1% | 20 | 21.9 / 21.6 | 16.8 / 16.6 | 21.8 / 21.4 | 17.0 / 16.9 |
| normal | 1% | 30 | 27.2 / 26.8 | 23.2 / 22.9 | 27.1 / 26.7 | 23.5 / 23.2 |
| normal | 2% | 20 | 57.4 / 56.8 | 49.4 / 49.0 | 56.7 / 56.1 | 50.4 / 50.2 |
| normal | 2% | 30 | 73.4 / 72.7 | 69.0 / 68.4 | 73.2 / 72.5 | 69.2 / 68.7 |
| Student-t(3) | 1% | 20 | 29.8 / 29.3 | 27.1 / 26.6 | 32.0 / 31.2 | 22.7 / 22.5 |
| Student-t(3) | 2% | 20 | 69.2 / 68.7 | 62.8 / 62.5 | 67.9 / 67.4 | 63.4 / 63.2 |
| Student-t(3) | 2% | 30 | 79.3 / 79.0 | 74.8 / 74.6 | 77.3 / 77.0 | 76.8 / 76.6 |
| skewed | 1% | 20 | 23.9 / 23.0 | 17.1 / 16.2 | 24.2 / 21.8 | 18.8 / 18.6 |
| skewed | 2% | 20 | 63.1 / 60.7 | 54.0 / 51.2 | 63.5 / 58.9 | 52.4 / 51.9 |
| contaminated | 1% | 20 | 34.5 / 34.0 | 32.2 / 31.8 | 37.9 / 36.8 | 25.1 / 25.0 |
| contaminated | 2% | 20 | 68.0 / 67.8 | 63.6 / 63.4 | 66.7 / 66.5 | 63.3 / 63.2 |

Power of the over-rejecting methods is inflated by their excess false rejections, so it is not a
like-for-like benefit. Mean over 4 shapes × 4 means: n = 20 percentile 46.0%, bootstrap-t 40.4%,
BCa 46.3%, Student-t 39.1%; n = 30: 54.6 / 50.3 / 54.1 / 50.4%.

### 1d. Cost

Best of 5 at B = 2000: n = 100 takes 1.2 ms (percentile), 2.1 ms (bootstrap-t), 1.3 ms (BCa),
1.5 ms (Student-t); n = 20 under 1 ms. Condition 3 is met by all four by three orders of magnitude.
All four are deterministic given the seed (no second random stream is needed).

## 2. Recommendation under the selection rule

**Nothing qualified.** Condition 1 (rejection rate ≤ 5.49% at every n = 20–50 on every mean-zero
shape) is met by no candidate:

| candidate | cells failing (of 20) | worst level | where |
|---|---|---|---|
| percentile bootstrap | 20 | 9.5% | skewed, n = 20 |
| BCa | 20 | 13.5% | contaminated, n = 20 |
| bootstrap-t | 10 | 9.7% | contaminated, n = 50 (fails Student-t(3) and contaminated; passes normal and skewed) |
| Student-t | 5 | 8.0% | skewed, n = 25 (fails skewed only; passes normal, Student-t(3), contaminated) |

The rule was not loosened. The best candidate by worst-case level is the **classical Student-t**
interval at **8.0%** (skewed, n = 20–25; 6.4–6.9% at n = 30–50). It is also the only one that never
exceeds nominal on heavy tails (4.2–4.6% on Student-t(3), 3.6–4.2% on contaminated) but is
conservative there. Bootstrap-t is the opposite: close to 5% on skew (5.0–5.3%) but 7.3–9.7% on heavy
tails. The two failures are complementary, and **no single candidate fixes both**.

Alternatives for Alfred (neither is mine to decide):

- **Higher minimum n for a verdict.** From the table, no n up to 100 brings any method inside the
  limit on every shape (percentile at n = 100: 5.9–6.1%; Student-t on skew at 100: 5.9%), so raising
  `MIN_EVENTS_VERDICT` alone does not meet the rule within the grid. It would also push verdicts
  further beyond what NVDA and PFE can reach (MR-003: max 15 events).
- **Report the measured level instead of a nominal one.** State in the methodology that the interval
  is approximately 95% and that simulated false-detection is about 4–8% for n = 20–50 depending on
  shape, with Student-t reducing it on symmetric heavy-tailed data. Spec section 15 already forbids
  publishing an *assumed* rate, so a measured range fits it.
- An untested idea, not evaluated and not proposed: a rule requiring both bootstrap-t and Student-t to
  exclude zero would be conservative on both failure shapes, but it was not a pre-registered candidate
  and its level has not been measured.

## 3. What changes for a user

The table compares baseline against the best candidate (Student-t) so the effect is visible; it is
**not** a recommendation to switch. `detected` rate on a true-null regime, full rule:

| shape | n = 20 before → after | n = 30 | n = 50 |
|---|---|---|---|
| normal | 7.6% → 5.2% | 6.0% → 4.7% | 5.5% → 4.8% |
| Student-t(3) | 7.6% → 4.2% | 6.9% → 4.4% | 6.2% → 4.4% |
| contaminated | 8.1% → 3.6% | 7.6% → 3.8% | 7.2% → 4.1% |
| skewed | 9.4% → 7.9% | 7.5% → 6.8% | 6.0% → 6.0% |

Power given up (interval alone, true mean ±2%, n = 20): normal 57.4% → 50.4%, Student-t(3)
69.2% → 63.4%, skewed 63.1% → 52.4%, contaminated 68.0% → 63.3%. At ±1% and n = 20 the loss is
about 5 pp on symmetric shapes. Averaged over shapes and means, power falls from 46.0% to 39.1%
(n = 20) and from 54.6% to 50.4% (n = 30). Some of the baseline's power is its excess false rate.

Note that on today's corpus no regime reaches n = 20, so no user sees a `detected` label either way.

## 4. Proposed spec text (final, for the coordinator to apply at integration)

Decision: Student-t interval at every horizon, measured levels disclosed, zero-width rule. The spec
is not edited here.

**Section 10, replace the "Bootstrap" block:**

```diff
-Bootstrap:
-- percentile bootstrap;
-- deterministic fixed seed derived from methodology version;
-- default `B = 2000`, unless validation demonstrates a compelling implementation reason to change it before `mr-v1` is frozen.
+Interval for the mean:
+- classical Student-t interval, `mean +/- t(0.975, n-1) * s / sqrt(n)` with `s` the sample standard
+  deviation (n-1 divisor). It is deterministic and uses no random seed or resampling.
+- it is approximate, not an exact 95% interval. MR-008 measured how often it excluded zero on
+  simulated mean-zero returns (standard deviation 4.31%, 8,000 trials per cell, Monte-Carlo
+  standard error about 0.25 percentage points) for n = 20-50:
+    - normal: 4.9-5.3%;
+    - heavy-tailed (Student-t, 3 degrees of freedom): 4.2-4.6%;
+    - contaminated (90% small moves, 10% five times larger): 3.6-4.2%;
+    - skewed (exponential, skewness 2): 6.4-8.0%.
+  The measured rate is above the nominal 5% for skewed returns and below it for heavy-tailed ones.
+  Real market-adjusted returns were not used to choose the method and may behave differently.
+  Do not describe the interval as having exactly 95% coverage or a 5% false-positive rate.
+- the same method is used at every horizon, including day 0.
+- zero-width rule: if the interval cannot be computed from the sample (fewer than two events,
+  a non-finite value, no spread among the values) or its lower and upper bounds are equal, the
+  interval is reported as degenerate, spans zero, and never counts as excluding zero. This holds
+  for any interval method and is checked both when the interval is built and when the verdict is
+  applied.
+- the percentile bootstrap, bootstrap-t and BCa remain selectable in code for research, with
+  seeds derived from the methodology version; they are not used for a verdict.
+- the result records the interval method; the bootstrap resample count is reported only for a
+  method that resamples.
```

**Section 11, "Eligible for verdict", condition 2:**

```diff
-2. bootstrap 95% CI for the mean excludes zero;
+2. the interval for the mean (section 10) excludes zero, and is not degenerate or zero-width;
```

No other condition, threshold, minimum count, the 0.5% floor, split-half or mean/median rule changes.
In section 11 "Preliminary", nothing changes.

## 5. Day-0 and path-horizon intervals

Use **the same method** at every horizon, including day 0. Reasons: the path chart draws all horizons
as one band, so mixing methods would make width differences between day 0 and +1 an artefact of the
method; the engine already shares one `summarize_returns` for all of them; and day 0 has the smaller
exact-timing cohort, where the baseline's small-sample anticonservatism is largest (n = 10–15: 8.6–13.7%).
Only the +5 interval feeds the verdict. The other horizons are descriptive, so the choice for them is
presentational, but one method is simpler and cannot be misread.

## 6. Switching the default (done in the second pass)

- `DEFAULT_INTERVAL_METHOD` in `market_reaction/models.py` is now `IntervalMethod.STUDENT_T`. The
  engine passes no method, so every regime, path point (day 0 included) and the primary statistic
  follow it.
- `ReturnStatistics.interval_method` is a required field with no default, so a stored interval cannot
  be read as another method.
- `MarketReactionResult.bootstrap_resamples` is `None` when the default method does not resample (the
  smallest change that stops a Student-t interval looking bootstrap-produced).
- Existing tests that changed: `test_result_carries_version_provenance_and_round_trips` (the resample
  count is now `None`). The two MR-008 tests asserting the old default were rewritten. No other test
  pinned a percentile interval end to end.
- `scripts/mr003_nominal_level.py` and `mr003_validate.py` now measure Student-t unless told otherwise;
  the docstring of the former says so. MR-003's quoted 6-9% figures describe the old default.
- A methodology-version decision remains for the coordinator: `mr-v1` is not frozen, so the amendment
  can land before the freeze.

## Findings outside the packet's scope

- The original default accepted a zero-width percentile interval: 25 identical +1% returns gave
  `detected` on main. The second pass closes it for every method, at the interval and at the verdict
  (section 4), and pins it with tests including an engine run on identical returns.
- `ReturnStatistics` serialises two extra fields, `interval_method` and `interval_degenerate`.
