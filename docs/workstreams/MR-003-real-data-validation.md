# MR-003 — Historical Market Reaction Real-Data Validation

Status: BLOCKED on MR-006 (report delivered 2026-10-06; thresholds provisional, nothing frozen, positive regime not run)  
Owner: TBD (one worker; one reviewer pass is warranted for this stream)  
Depends on: MR-001 and MR-002 (both in main)  
Worktree: `C:\Dev\MS-worktrees\validation`

## Objective

Run the merged `mr-v1` engine on the real MarketSentinel corpus, freeze the two regime thresholds
from sentiment marginals **before any return outcome is inspected**, measure the procedure's actual
placebo/lag/stability behaviour, and give Alfred an evidence-backed go/no-go plus a frozen result
contract that MR-004 and MR-005 can build against.

This is a validation workstream. It does not add product surface and does not redesign `mr-v1`.

## Scope decided by Alfred (2026-10-06, `docs/DECISIONS.md`)

- Validate on **NVDA and PFE only**. Do not run a backfill; that is decided after this result.
- The London (LSE) path is **untested in validation**: no London-listed company has stored
  articles. Say so explicitly in the report. Do not present calendar or `CUKX.L` alignment checks
  as validation of the London path.
- The amended spec is ratified as `mr-v1`. Alfred's approval of this workstream's integration is
  the freeze.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md`
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md` (as amended 2026-09-19)
- `docs/DECISIONS.md` (2026-09-19 entry: four amendments)
- `docs/research/MR-001-data-readiness-report.md`
- `src/marketsentinel/market_reaction/`

Do not reinterpret or redesign approved product/methodology decisions.

## Required order of work

The order is part of the contract. Do not reorder it.

### Phase 1 — threshold pre-registration (sentiment only)

1. Build session signals for every company with stored real sentiment-scored articles, reading
   `data/marketsentinel.db` read-only.
2. Compute `tau_positive` / `tau_negative` with the spec's selection procedure over the pooled
   population `E`. Record: both thresholds, both observed tail shares, the population size, the
   per-company contribution to `E`, and the database snapshot date.
3. Write these to the validation report **before** any code path that reads a price or return is
   run. No return, price, or event outcome may be loaded in this phase.
4. The spec does not say whether companies that fail history sufficiency (AAPL, MSFT) belong in
   `E`. Compute the thresholds both ways and record both. If the rounded thresholds agree, proceed.
   If either differs, escalate. Do not pick the one that yields more events.

### Phase 2 — real inputs

5. Fetch adjusted daily closes for the validated companies and for `SPY` (and `CUKX.L` for
   calendar/benchmark alignment checks) through the existing price path. This is a free network
   read performed once by a script, never by a test.
6. Freeze what tests need as a sanitized fixture under `tests/fixtures/market_reaction/`. The
   existing `nvda_pfe_real_sample.json` carries `^GSPC` / `^FTSE` only and must not be edited;
   add a new fixture file for the tracker series.

### Phase 3 — validation with the pre-registered thresholds

7. History sufficiency on real data: observed span, density, and coverage per company against
   `>= 126`, `>= 0.50`, `>= 63`. State whether the three-condition rule is practical.
8. Event counts per company and regime under the pre-registered thresholds: tail sessions →
   `>= 3` sources → after exclusivity → resolved at +5.
9. Full engine result per company: evidence state per regime, mean/median/n, CI, split-half,
   day-0 cohort `n_day0` vs forward `n`.
10. Spec §15 validation, measured not assumed:
    - synthetic null;
    - permutation/placebo on real event sessions;
    - one-session lag-shift sensitivity;
    - split-half stability;
    - manual face-validity inspection of a sample of events back to their article IDs.
11. Report the measured placebo detection rate. Do not publish an assumed false-positive rate.
12. Quantify the data-lineage risks MR-001 raised where they bear on the verdict: lagged-event
    share, the two ingestion regimes inside the chronological split, and relevance noise inside
    tail sessions.

### Phase 4 — freeze proposal

13. Set `MR_V1_FROZEN_THRESHOLDS` in the branch to the Phase 1 values, with tests. Alfred's merge
    is the freeze; until then it is a proposal.
14. Document the result contract MR-004/MR-005 will consume: the fields of the versioned result
    object, per-horizon cohort and `n`, the evidence states, and anything a consumer must not infer.

## Allowed scope

- validation/inspection scripts under `scripts/`;
- `docs/research/MR-003-validation-report.md`;
- new sanitized fixtures under `tests/fixtures/market_reaction/`;
- offline tests for the frozen thresholds and the new fixture;
- the `MR_V1_FROZEN_THRESHOLDS` constant;
- code-level bug fixes in `src/marketsentinel/market_reaction/`, each with a regression test.

## Do not touch

- the methodology: signal formula, timing semantics, threshold selection procedure, either floor,
  benchmark instruments, primary horizon, evidence/verdict rules, history-sufficiency conditions;
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md` and `docs/DECISIONS.md`;
- `docs/planning/` and other workstreams' packets;
- the existing frozen fixtures;
- API, snapshot publication, frontend, R2/request system;
- the SQLite schema; the database is opened read-only;
- coverage budgets, backfill, LLM prompts/contracts.

## Required output / interface

- `docs/research/MR-003-validation-report.md`;
- `C:\Dev\MS-shared\reports\MR-003.md` using `docs/workstreams/REPORT_TEMPLATE.md`;
- frozen thresholds proposed in code;
- the documented result contract.

## Acceptance criteria

- [ ] thresholds were computed from sentiment marginals only and recorded before any return was
      read, and the report shows how that ordering was enforced;
- [ ] both thresholds, both tail shares, and the population are reported;
- [ ] neither threshold was loosened to recover event counts;
- [ ] history sufficiency (span, density, coverage) is reported per company with observed values;
- [ ] event funnel and evidence state are reported per company and regime;
- [ ] synthetic null, permutation/placebo, lag-shift, and split-half results are measured and
      reported;
- [ ] face-validity sample is traced to article IDs;
- [ ] benchmark is `SPY` / `CUKX.L`, not a price index;
- [ ] the report states that the London (LSE) path is untested in validation;
- [ ] `MR_V1_FROZEN_THRESHOLDS` is set in the branch and covered by tests;
- [ ] the result contract for MR-004/MR-005 is written down;
- [ ] every test depends only on files under `tests/fixtures/`; none reads `C:\Dev\MS-shared\`,
      the live database, or the network;
- [ ] focused market-reaction tests, full Python suite, `ruff check`, `ruff format --check` pass;
- [ ] no paid LLM calls; no database writes;
- [ ] the report ends with a recommendation: GO / GO WITH LIMITS / NO-GO, and the reasons.

## Autonomy

Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Fix in-scope bugs you discover. Add
regression tests. Do not stop for cosmetic choices.

A disappointing result is a result. If no regime reaches `detected`, or only one company is
eligible, report that plainly. Do not adjust thresholds, windows, or sufficiency rules to change it.

## Escalate only if

- the pre-registered thresholds cannot be determined uniquely by the spec;
- validation shows the approved methodology is misleading on real data;
- a methodology rule would have to change to make the feature usable;
- a persistent-schema change is unavoidable;
- public/private or paid-spend behaviour would have to change.

Record the issue in the report and continue all unblocked work before escalating. The go/no-go
itself is Alfred's decision; the worker recommends.

## Git rules

Do not push, merge, rebase, reset, force checkout another worktree/branch, or modify main.  
No commits; Alfred performs Git writes.

## Completion report

Write `C:\Dev\MS-shared\reports\MR-003.md` from `docs/workstreams/REPORT_TEMPLATE.md`, and include:

1. **Recommendation:** GO / GO WITH LIMITS / NO-GO
2. pre-registered thresholds and how outcome-blindness was enforced
3. per-company history sufficiency and event funnel
4. per-regime evidence states
5. placebo / lag-shift / split-half measurements
6. frozen result contract
7. assumptions still unvalidated
8. decisions needed from Alfred
