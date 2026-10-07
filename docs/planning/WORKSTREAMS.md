# MarketSentinel Workstreams

Coordinator-owned control plane.  
Only Alfred (or the designated coordinator acting on Alfred's instruction) updates statuses here.

## Current milestone

**M8 — Historical Market Reaction (`mr-v1`)**

Goal: establish a leakage-safe, benchmark-adjusted historical market-reaction engine that becomes the quantitative foundation for later event-type impact and historical-analogue features.

## Active / queued work

| ID | Workstream | Owner | Status | Depends on | Worktree | Output |
|---|---|---|---|---|---|---|
| MR-001 | Data readiness | Claude Code A | DONE | none | removed | readiness report + real fixture — in main as `b830183` |
| MR-002 | Quant core | Claude Code B | DONE | none | removed | deterministic engine + tests — merged to main 2026-10-06 |
| MR-003 | Real-data validation | TBD (rerun needs a new worktree) | BLOCKED | schema rollout, label pilot + review, months 13–36 news backfill, role labels for all 36 months | removed | report GO WITH LIMITS (provisional); report, scripts and tracker fixture in main as artifacts; nothing frozen |
| MR-004 | API + snapshot integration | TBD | BLOCKED (on hold: data first) | MR-003 contract freeze | created later | public reaction block + endpoint; must settle price persistence |
| MR-005 | Frontend | TBD | BLOCKED (on hold: data first) | MR-003 contract freeze | created later | Historical Market Reaction UI |
| MR-006 | Primary-company pre-analysis | Claude Code worker | DONE | none | removed | company-role stage, label storage (`user_version` 6), engine filter — merged to local main 2026-10-07; spends nothing until caps are raised |
| MR-007 | GDELT investigation for deeper history | Claude Code worker | REVIEW (criteria met with one flagged deviation; awaiting Alfred's approval to integrate) | none | `C:\Dev\MS-worktrees\gdelt` (`ms/mr-007-gdelt`), work uncommitted | whether 24+ months of history is obtainable; report + recommendation, no backfill |
| MR-009 | Backfill start offset and run plan (months 13–36) | TBD | READY (not started) | none to draft; the backfill itself waits for the rollout and the pilot | `C:\Dev\MS-worktrees\backfill-offset` (not yet created) | small offset change + run plan, drafted offline for Alfred's approval; fetches and writes nothing |
| MR-008 | Bootstrap small-sample false-alarm fix | Claude Code worker | DONE | none | removed | Student-t is the default interval; zero-width intervals never count as evidence; measured levels disclosed in the spec — merged to local main 2026-10-07 |

### After MR-007 (2026-10-07) — current plan

Decisions are in `docs/DECISIONS.md` (2026-10-07, "After MR-007").

- **Backfill approved:** Google News RSS, months 13–36, NVDA and PFE, same 30-day window regime, no
  re-fetch of the stored 12 months. It needs the start-offset change drafted and approved first:
  `docs/workstreams/MR-009-backfill-start-offset.md`.
- **Date-only timestamps accepted** for verdicts. The bulk-file route is not to be scoped.
- **Verdict wording requirement for MR-004 and MR-005:** results describe moves from the day after
  publication, not the same-day reaction. Not yet written into the spec's claims policy.
- **GDELT check from GitHub Actions:** later, non-blocking, a few spaced requests. No packet.
- **Still open for Alfred, to be proposed by MR-009:** where the backfill runs against the live
  corpus in R2, whether it performs any paid Stage A/B/C analysis, and the role-label budget for
  the new articles.
- **Critical path, in order:**
  1. schema-6 rollout (`docs/planning/SCHEMA_6_ROLLOUT.md`) — Alfred;
  2. pilot of 200 labels, then Alfred's review of 30 (at least 27 correct) — Alfred;
  3. role-label backfill of the stored 12 months, NVDA and PFE — Alfred;
  4. MR-009 approved and integrated (can be drafted in parallel with 1–3);
  5. news backfill of months 13–36, then role labels for those articles — Alfred;
  6. MR-003 resumes on the full labelled corpus: outcome-blind filter-placement check, threshold
     re-selection, positive regime, freeze decision. Needs a refreshed packet;
  7. MR-004 and MR-005.

### After MR-003 (2026-10-06)

Decisions are in `docs/DECISIONS.md` (three 2026-10-06 entries).

- **Data first.** MR-004 and MR-005 are on hold until the data is fit to show.
- **Nothing is frozen.** `tau_positive = 0.42` / `tau_negative = 0.20` (pooling A) are provisional;
  `MR_V1_FROZEN_THRESHOLDS` is unset; positive-regime outcomes are unmeasured.
- **MR-003 is not DONE.** Open against its packet: thresholds not frozen, positive regime not run.
  Its report, scripts and tracker price fixture were integrated into main on 2026-10-07 as
  artifacts only. Report: `C:\Dev\MS-shared\reports\MR-003.md`.

### After MR-006 (2026-10-07)

Decisions are in `docs/DECISIONS.md` (2026-10-07 entry).

- **MR-006 is DONE and merged to local main** with MR-003's artifacts. Checked on merged main with
  the CI install: 1164 tests pass, `ruff check` and `ruff format --check` clean, materiality
  evaluation PASS. Report: `C:\Dev\MS-shared\reports\MR-006.md`. Proposal and runbook:
  `docs/research/MR-006-proposal.md`.
- **The approved spec amendment is applied** (company-role eligibility, spec section 2.1).
- **Main is now a schema-6 build and is not pushed.** Pushing starts the rollout, in this order:
  refresh the baked fallback snapshot, disable the schedule, push, deploy the public build, dispatch
  one worker run with every role cap at `0`, re-enable the schedule. Push and deploy are Alfred's.
- **Nothing has been spent and no real label exists.** Role caps default to `0`.
- **Operational steps before MR-003 can resume, all Alfred's (runbook section 5):**
  1. schema rollout as above;
  2. pilot dispatch of 200 labels, NVDA and PFE;
  3. Alfred reviews a sample of the pilot labels — nothing larger runs before this;
  4. bulk backfill in dispatches of at most 1,000, NVDA and PFE only;
  5. raise the steady-state defaults (10 per ticker, 25 total) in a reviewed commit.
- **Then MR-003 resumes:** outcome-blind check of placement (a) versus the (b) fallback, threshold
  re-selection on the labelled pool, positive regime, freeze decision. It needs a refreshed packet.
- **Rollout checklist with exact commands:** `docs/planning/SCHEMA_6_ROLLOUT.md`.
- **Pilot acceptance rule (Alfred, 2026-10-07):** Alfred reviews 30 pilot labels drawn at random
  with a pre-fixed seed; the bulk backfill proceeds only if at least 27 are correct. Result: not
  yet run.
- **Packets drafted 2026-10-07:** `docs/workstreams/MR-008-bootstrap-small-sample-fix.md` and
  `docs/workstreams/MR-007-gdelt-deeper-history.md`. MR-008 chooses its method on synthetic data
  only and does not switch the default. MR-007 is permitted small read-only probes of GDELT and
  Google News RSS and writes nothing to the database.
- **MR-008 reviewed 2026-10-07: REVIEW, not integrated.** Report
  `C:\Dev\MS-shared\reports\MR-008.md`; proposal `docs/research/MR-008-proposal.md` in the worktree.
  Confirmed in the worktree: 1192 tests pass, `ruff check` and `ruff format --check` clean; only
  `statistics.py` and `models.py` changed under `src/`; the spec, `docs/DECISIONS.md`,
  `docs/planning/`, packets and fixtures untouched; no new test or script reads a fixture, the
  database or `C:\Dev\MS-shared\`; `MR_V1_FROZEN_THRESHOLDS` unset. **Default diffed against main
  by the coordinator:** 2,273 seeded synthetic samples, including degenerate ones, give
  byte-identical intervals, statistics and states on both trees. **Unmet criterion:** degenerate
  inputs are not safe on the default path — on main and on the branch, 25 identical returns of 1%
  give the interval `[0.01, 0.01]` and the state `detected`. **Alfred's decision**
  (`docs/DECISIONS.md`): Student-t becomes the default, the measured levels are disclosed, and no
  method may count a zero-width interval as evidence. The packet's second-pass section covers it.
- **MR-008 second pass reviewed and integrated 2026-10-07: DONE.** In the worktree before the
  merge: 1215 tests pass, ruff clean. Coordinator probe on synthetic inputs: for all four methods,
  identical, single, two-equal, `nan` and `inf` samples never reach `detected` or `unstable` and are
  flagged degenerate; a hand-built zero-width or degenerate-flagged statistic is refused by the
  verdict rule; `interval_method` is required; Student-t bounds match SciPy to 4e-15; a clear
  synthetic effect is still `detected`; the percentile function is textually identical to main's.
  Thresholds, minimum counts and the 0.5% floor are unchanged; `MR_V1_FROZEN_THRESHOLDS` is unset.
  Spec sections 10 and 11 amended as proposed. One result-contract change to know for MR-004:
  `bootstrap_resamples` is now `null` when the method does not resample.
- **MR-007 reviewed 2026-10-07: verdict 24+ months OBTAINABLE WITH LIMITS.** Report
  `C:\Dev\MS-shared\reports\MR-007.md`; write-up `docs/research/MR-007-gdelt-report.md` in the
  worktree. Google News RSS returned a full 100-entry page for 30-day windows 13, 18, 24 and 36
  months back for NVDA and PFE, 91–98% date-only. GDELT DOC 2.0 answered HTTP 429 on the first
  request of two runs, so its content is unmeasured. Confirmed in the worktree: 1215 tests pass,
  ruff clean, no tracked file changed, the local database untouched. Deliverables are a report and a
  probe script only. **Flagged deviation:** one request went to a third host,
  `data.gdeltproject.org` (a single bulk-file sample); the packet permitted two endpoints, and the
  report says Alfred approved that request at the keyboard. **Not validated:** anything about GDELT
  content; Google for AAPL, MSFT, AMZN or London names; the corrected bulk-file column code was
  never run. **Awaiting Alfred:** whether to backfill months 13–24, whether date-only timing is
  acceptable for verdicts, whether to keep pursuing GDELT.
- **AAPL and MSFT** are neither activated nor labelled; revisit after MR-007.
- **Real label quality is unvalidated** until the pilot review: every test used a scripted provider.
### Reconciliation notes (2026-10-06)

- **MR-001:** verdict READY WITH GAPS. In main as `b830183`. Worktree and branch removed.
- **MR-002:** merged to local main, including all four approved amendments (asymmetric thresholds,
  exact-only day 0, three-condition history sufficiency, `SPY` / `CUKX.L`) in the engine, tests,
  spec, and `docs/DECISIONS.md`. Checked on merged main with the CI install
  (`uv sync --locked --all-extras --dev`): 1045 tests pass, `ruff check` and `ruff format --check`
  clean. Worktree and branch removed. No completion report exists (the
  September session predates the report rule).
- **Spec:** the amended spec is ratified as `mr-v1` (`docs/DECISIONS.md`, 2026-10-06).
- **MR-003:** see "After MR-003" above. Packet: `docs/workstreams/MR-003-real-data-validation.md`.
  Scope was NVDA and PFE only; the London (LSE) path is untested in validation.
- **MR-004 carry-forward:** price persistence was deferred to MR-004 and must not slip past it. A
  public result that cannot be reproduced is a credibility problem for this product.
- **Not yet pushed:** local main is ahead of `origin/main`. Pushing is Alfred's.

## Execution rule

### Wave 1
Run MR-001 and MR-002 in parallel.

MR-001 should publish the smallest useful real fixture as early as possible so MR-002 can test against real timestamp/price/calendar behavior without waiting for the entire readiness report.

### Validation gate
MR-003 is sequential and consequential.

Do not freeze API/frontend contracts until real-data validation has confirmed the methodology shape.

A principal-model/Fable review is warranted only if MR-003 exposes a genuine methodology decision.

### Wave 3
After MR-003 freezes the result contract, run MR-004 and MR-005 in parallel.

## Concurrency policy

- Maximum simultaneous MarketSentinel implementation agents: 2.
- Suggested maximum simultaneous coding agents across all projects: 3.
- More parallelism requires evidence that Alfred's review/integration queue is not becoming the bottleneck.

## Integration policy

- Main repo remains clean/deployable.
- Each active agent works in an isolated Git worktree.
- Workers perform no Git writes and never modify main.
- The coordinator may perform local Git operations — commit, merge into local `main`, create and remove worktrees and branches — after Alfred approves each integration. Approval is per integration.
- No agent ever pushes, force-pushes, rebases, runs `reset --hard`, or deploys. Those stay with Alfred.
- Revisit branch-local worker commits only if that becomes a demonstrated bottleneck.

## Agent autonomy

Workers continue until acceptance criteria pass.

Do not stop for:
- naming;
- cosmetic preferences;
- ordinary local refactors;
- in-scope bugs;
- obvious implementation choices.

Escalate only for:
- methodology changes;
- product-semantic changes;
- public/private security-boundary changes;
- paid-spend policy;
- incompatible persistent-schema decisions;
- blockers that invalidate the workstream contract.

## Review rhythm

Prefer one scheduled integration/review window per day rather than interrupt-driven supervision.

For each completed workstream:
1. read the worker report;
2. run/confirm objective checks;
3. optionally run the standard reviewer prompt;
4. spot-check key acceptance criteria;
5. Alfred approves the integration; the coordinator commits/merges locally; Alfred pushes;
6. update this table;
7. launch newly unblocked work.
