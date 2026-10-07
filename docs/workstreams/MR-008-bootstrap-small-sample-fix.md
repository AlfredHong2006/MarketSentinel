# MR-008 — Bootstrap Small-Sample False-Alarm Fix

Status: DONE (both passes merged to local main 2026-10-07)  
Owner: TBD  
Depends on: none  
Worktree: `C:\Dev\MS-worktrees\bootstrap`

## Objective

Find an interval method for the mean that holds its nominal 5% false-detection level at the sample
sizes `mr-v1` actually reaches a verdict on (`n = 20–50`), implement it as a selectable option
without changing today's results, and propose the methodology amendment for Alfred's approval.

MR-003 measured the current percentile bootstrap rejecting a true zero mean at about 6–9% for
`n = 20–30` against a nominal 5% (5.4–6.8% at `n = 50`). The verdict rule's other conditions remove
almost none of those false detections. Changing the interval method is a methodology change, so
this workstream ends with a proposal; it does not switch the default.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md`
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`, sections 10, 11, 15, 17 and 19
- `docs/DECISIONS.md`, the 2026-10-06 entry "Later market-reaction workstreams"
- `docs/research/MR-003-validation-report.md` and `C:\Dev\MS-shared\reports\MR-003.md`, section F2
- `scripts/mr003_nominal_level.py`
- `src/marketsentinel/market_reaction/statistics.py`, `models.py`, `engine.py`

Do not reinterpret or redesign approved product/methodology decisions.

## Hard limits

- **Synthetic data only.** The method is chosen on simulated returns with a known true mean. Do not
  load, compute, or look at any real return, real event outcome, or real evidence state — not from
  the database, not from a fixture, not by running `scripts/mr003_validate.py`. Choosing an interval
  method while looking at real outcomes would be the outcome-peeking `mr-v1` forbids.
- No network, no LLM or API call, no API key, no database access, no Git writes.
- Do not set `MR_V1_FROZEN_THRESHOLDS`.

## Work

### 1. Reproduce the problem

Reproduce MR-003's measurement of the current method: the rate at which the 95% interval excludes
zero, and the rate at which the full five-condition rule returns `detected`, on mean-zero data.
State the Monte-Carlo standard error of every rate.

### 2. Evaluate candidates

At minimum compare, on identical simulated samples:

- the current percentile bootstrap (baseline);
- the studentized (bootstrap-t) interval;
- BCa;
- the classical Student-t interval for the mean.

Add another candidate only with a stated reason.

Grid, at minimum:

- `n` = 10, 15, 20, 25, 30, 40, 50, 75, 100;
- distributions with true mean zero: normal; Student-t with 3 degrees of freedom; one clearly
  skewed distribution; one contaminated mixture (mostly small moves plus rare large ones), all
  scaled to a daily-return-like spread (MR-003 used σ = 4.31%);
- power against a true mean of ±1% and ±2% for the same shapes.

Use enough trials that the Monte-Carlo standard error at a 5% rate is at most 0.25 percentage
points, and say how many that is. Every simulation is seeded and reproducible.

For each cell report: the interval-alone rejection rate, the full-rule `detected` rate, average
interval width, and power.

### 3. Selection rule (fixed here, before any result)

Recommend the candidate that satisfies all of:

1. on every mean-zero distribution at every `n` from 20 to 50, its interval-alone rejection rate is
   no more than 5% plus two Monte-Carlo standard errors;
2. it is not needlessly conservative: where that holds for more than one candidate, prefer the one
   with the highest power at `n = 20` and `n = 30`;
3. it is deterministic given the methodology seed, and costs no more than about one second per
   regime on a laptop at the default resample count;
4. ties go to the simpler method.

If **no** candidate meets condition 1 on the heavy-tailed or skewed shapes, do not loosen the rule.
Report that plainly, with the best candidate's measured level, and set out the alternatives
(a higher minimum `n` for a verdict; reporting the measured level instead of a nominal one) for
Alfred. Raising the minimum `n` is a methodology decision and is not yours to make.

### 4. Implementation

- Add the candidate methods to `market_reaction/statistics.py` behind an explicit, typed
  `interval_method` option.
- **The default stays the current percentile bootstrap.** With the default, every existing test and
  every engine result must be byte-identical to main.
- The result object records which interval method produced the interval.
- Seeding stays a pure function of the methodology version and scope; a method that needs a second
  random stream derives it the same way.
- Keep the module pure: no I/O, no clock, no unseeded randomness.
- Tests: exact values on small hand-checkable inputs; determinism; order-independence where the
  engine promises it; degenerate inputs (`n = 1`, zero variance, identical values) return a typed,
  safe result and never a `nan` interval that could read as "excludes zero"; a seeded regression
  test pinning the measured level of the recommended method at `n = 20` within a stated tolerance.

### 5. Closing proposal

Write `docs/research/MR-008-proposal.md`:

1. the measured tables;
2. the recommendation under the selection rule, or the plain statement that nothing qualified;
3. what changes for a user: how often a true-null regime would be labelled `detected` before and
   after, and what power is given up;
4. the exact proposed text change to spec section 10 (and section 11 if needed), as a diff-style
   block. Do not edit the spec;
5. whether the day-0 and path-horizon intervals should use the same method, with the reason;
6. what switching the default would involve (the one constant, the tests that then change).

## Second pass (added 2026-10-07, after Alfred's decision)

The first pass found that no candidate met the selection rule. Alfred decided
(`docs/DECISIONS.md`, 2026-10-07, "Interval method"): adopt Student-t as the default, disclose the
measured levels, and make a zero-width interval unable to count as evidence under any method.
This section supersedes "the default stays the current percentile bootstrap" above.

The first pass left one acceptance criterion unmet: degenerate inputs are not safe on the default
path. On main, 25 identical returns of 1% give the interval `[0.01, 0.01]` and the state `detected`.

Second-pass work:

1. `DEFAULT_INTERVAL_METHOD` becomes Student-t. It applies at every horizon, including day 0.
2. **Zero-width guard, for every method including the percentile bootstrap,** enforced twice:
   - at the interval: fewer than two observations, non-finite values, no spread, or a computed
     interval whose bounds are equal, all return the degenerate zero-containing interval with
     `interval_degenerate = True`;
   - at the verdict: `ci_excludes_zero` returns `False` whenever the statistic is flagged degenerate
     or its bounds are equal, so no caller can reach `detected` or `unstable` through one.
3. `interval_method` on `ReturnStatistics` becomes a required field with no default, so a stored or
   hand-built statistic can never be silently relabelled when the default changes.
4. The result must not present a bootstrap resample count as if it produced a Student-t interval.
   Make the smallest change that removes the misleading reading, and report it.
5. Tests: the default is Student-t; every method, on every degenerate shape, never yields
   `detected` or `unstable` through the full `regime_state` rule; a constructed case where a
   bootstrap method's bounds coincide; the engine end to end on a synthetic regime of identical
   returns.
6. Update `docs/research/MR-008-proposal.md` section 4 with the final proposed spec text for
   sections 10 and 11: the Student-t method, the measured levels by shape, the word "approximate",
   and the zero-width rule. The coordinator applies it to the spec at integration; do not edit the
   spec.
7. Update the quoted figures or comments in `scripts/mr003_nominal_level.py` only if they would now
   be wrong about what the script measures. Do not run `scripts/mr003_validate.py`.

Second-pass acceptance criteria, in addition to the ones below that still apply:

- [ ] the default is Student-t and the result records it;
- [ ] no method can produce `detected` or `unstable` from a zero-width or degenerate interval, shown
      by tests at both the interval and the verdict;
- [ ] `interval_method` is required on `ReturnStatistics`;
- [ ] no result field misdescribes how the interval was built;
- [ ] the proposal's section 4 holds the final spec text;
- [ ] still synthetic only: no real return, outcome or evidence state loaded or inspected.

"Default results byte-identical to main" no longer applies. What must stay identical: everything
that is not an interval bound or a state that depends on one.

## Allowed scope

- `src/marketsentinel/market_reaction/statistics.py`, `models.py`, `engine.py`, `__init__.py`;
- a simulation script under `scripts/`;
- tests under `tests/`;
- `docs/research/MR-008-proposal.md`.

## Do not touch

- thresholds, the threshold selection procedure, the role filter, timing semantics, benchmarks, the
  primary horizon, the minimum event counts, the 0.5% effect floor, the split-half and mean/median
  rules;
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`, `docs/DECISIONS.md`, `docs/planning/`, other
  workstreams' packets;
- frozen fixtures in `tests/fixtures/`;
- anything outside `market_reaction/`, `scripts/`, `tests/` and the proposal.

## Acceptance criteria

- [ ] MR-003's measurement of the current method is reproduced, with standard errors;
- [ ] every candidate is measured on the full grid with identical samples, seeded and reproducible;
- [ ] the recommendation follows the selection rule above, or the report states that no candidate
      qualified; the rule was not adjusted after seeing results;
- [ ] no real return, outcome, or evidence state was loaded or inspected, and the report says so;
- [ ] the candidate methods are selectable, the default is unchanged, and default results are
      byte-identical to main;
- [ ] the result object records the interval method;
- [ ] degenerate inputs are safe and tested;
- [ ] `MR_V1_FROZEN_THRESHOLDS` is still unset;
- [ ] `docs/research/MR-008-proposal.md` contains all six sections;
- [ ] focused tests, the full Python suite, `uv run ruff check .` and
      `uv run ruff format --check .` pass;
- [ ] no test makes a network call or reads the live database or `C:\Dev\MS-shared\`.

## Autonomy

Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Fix in-scope bugs you discover. Add
regression tests. Do not stop for cosmetic choices.

## Escalate only if

- a correct method cannot be made deterministic under the methodology seed;
- the fix would require changing a verdict condition other than the interval;
- a hard limit would have to be crossed.

Record the issue in the report and continue all unblocked work. The choice of method and any change
to the minimum `n` are the closing proposal, not escalations.

## Git rules

Do not push, merge, rebase, reset, commit, stage, force checkout another worktree/branch, or modify
main. No Git writes of any kind.

## Completion report

Write `C:\Dev\MS-shared\reports\MR-008.md` from `docs/workstreams/REPORT_TEMPLATE.md`, and include:

1. **Outcome**, with the recommended method or "none qualified"
2. the headline table: measured false-detection rate before and after at `n` = 20, 30, 50
3. files changed
4. checks run, with real results
5. confirmation that no real outcome was inspected
6. decisions needed from Alfred
7. assumptions still unvalidated — including that simulated return shapes are a stand-in for real
   market-adjusted returns, which this workstream deliberately did not look at
