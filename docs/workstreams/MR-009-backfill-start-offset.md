# MR-009 — Backfill Start Offset and Run Plan (Months 13–36)

Status: READY (not started; no worktree yet)  
Owner: TBD  
Depends on: none to draft and test; the backfill itself waits for the schema-6 rollout and the label pilot  
Worktree: `C:\Dev\MS-worktrees\backfill-offset` (create when started)

## Objective

Make it possible to backfill months 13–36 of Google News history for NVDA and PFE without
re-fetching the 12 months already stored, keeping the existing 30-day window regime, and write the
run plan for Alfred's approval.

This workstream drafts and tests a small change offline. It fetches nothing, writes to no real
database, and runs no backfill. The backfill is a separate operational step that Alfred runs after
approving the change and the plan, and only after the schema-6 rollout and the label pilot have
passed.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md`
- `docs/DECISIONS.md` — the 2026-10-07 entry "After MR-007", the 2026-10-07 company-role entries,
  and the 2026-09-13 entries on the analysis job ledger and scheduled coverage
- `docs/research/MR-007-gdelt-report.md`, sections 3, 4 and 5
- `docs/research/MR-001-data-readiness-report.md`, sections 2 and 7
- `docs/planning/SCHEMA_6_ROLLOUT.md`
- `scripts/backfill_historical_intelligence.py`, `src/marketsentinel/historical_backfill.py`,
  `backfill_service.py`, `sources/historical.py`, `analysis_ledger.py`, `coverage_cycle.py`

Do not reinterpret or redesign approved product/methodology decisions.

## Hard limits

- No network. No news fetch, no price fetch, no model download.
- No database writes outside throwaway test databases under `data/test-runtime/`. Do not run the
  backfill script against any real database, in any mode.
- No LLM or API call, no API key, do not read `.env`.
- No Git writes.

## Fixed by Alfred's decision

- Source: Google News RSS only.
- Range: months 13–36 counted back from the run date. The 12 most recent months are not re-fetched.
- Window regime: the buckets the backfill plans today. Do not shorten or change them; MR-007
  showed narrower windows return about 2.8 times the articles, which would mix sampling regimes
  inside one pooled distribution.
- Tickers: NVDA and PFE.

## Work

### 1. Establish whether a code change is needed at all

The script already accepts `--as-of`, which is used as "now" when planning buckets. Determine,
from the code and with tests, whether `--as-of <about 12 months ago> --months 24` already plans
exactly months 13–36 and nothing else, and what else `--as-of` changes: publication-time
validation, selection, ledger state, watermarks, anything recorded in reports. State plainly
whether using it this way is safe or a misuse of a flag meant for replaying a past run.

### 2. Draft the smallest change

If `--as-of` is not a safe way to do it, draft the smallest explicit option that skips the most
recent N months: an offset on the planning horizon, applied in the pure planner
(`historical_backfill.py`) and exposed by the script.

Requirements either way:

- with the option unset, the planned buckets and every existing test result are unchanged;
- bucket boundaries for the offset range are the ones a plain longer-horizon run would have planned
  for those months, so the deeper history lines up with any future full-horizon run;
- the planner stays pure: `now` is injected, no clock, no I/O;
- an offset that leaves no range, or exceeds the horizon, is rejected with a clear error;
- tests cover: the default; months 13–36 exactly; a run date on a month boundary; the first and last
  bucket being partial; no overlap with and no gap against the most recent 12 months.

The change is a draft for approval. Deliver it as working code and tests in the branch; it is not
integrated until Alfred approves it.

### 3. Answer the questions the run depends on

From the code, without running anything:

- **Where does the run execute?** The live corpus is the private database in R2, and the scheduled
  worker is its only writer. Set out the options (a manual workflow dispatch that runs the backfill
  inside the existing download → integrity check → checkpoint sequence; a local run against a
  downloaded copy with the schedule disabled, then an upload) with what each needs and what can go
  wrong. Recommend one. Do not add a workflow in this workstream unless the recommended option is
  a small addition to the existing one, and then only as part of the draft.
- **Does the backfill spend on Stage A/B/C?** The script selects analysis candidates per bucket and
  calls the provider when a key is configured. State exactly what a run does with and without a
  key, what it records for analysis when the key is unset, and how to guarantee a fetch-and-score
  run with no paid analysis. `mr-v1` needs sentiment-scored articles and role labels, not
  Stage A/B/C.
- **What ledger state do the new articles get?** Under the continuous-coverage rules, and what
  that means for later scheduled runs: will the worker then try to analyse 24 months of old
  articles under its normal caps?
- **Role labels.** How the new articles reach the role stage, in what order, and how many labels
  that is. Give the count as a formula on the article count and at MR-007's estimate of about 1,000
  articles per ticker per 12 months.
- **Deduplication and overlap** with what is stored, at the 12-month boundary.
- **Pacing.** The Google provider does not pace itself and resolves publisher redirects. State how
  many requests a 24-bucket run makes per ticker including redirects, and whether pacing needs to
  be added for a run of this size.
- **Concurrency.** The script warns against running alongside a coverage cycle for the same ticker.
  State how the recommended option prevents that.

### 4. Closing proposal

Write `docs/research/MR-009-proposal.md`:

1. the change, or the finding that none is needed, with the exact command for months 13–36;
2. the recommended place to run it and the alternatives;
3. the spend: paid analysis (recommended: none) and role labels, as counts and a cost formula at
   `gpt-4o-mini` list price ($0.15 per 1M input, $0.60 per 1M output tokens, checked 2026-10-07),
   using measured tokens per label if the pilot has run by then, otherwise both the MR-006 plan
   figure and a clear statement that it is unmeasured;
4. a runbook: the exact steps in order, what to check before and after each, how to verify that
   only months 13–36 were fetched, and how to roll back;
5. what the deeper history does to `mr-v1`: thresholds are re-selected on the larger labelled pool,
   and MR-003 resumes only after the backfill and its labels are complete.

## Allowed scope

- `src/marketsentinel/historical_backfill.py`;
- `scripts/backfill_historical_intelligence.py`;
- `src/marketsentinel/backfill_service.py` and `sources/historical.py`, only if the offset or
  pacing cannot be done without them, with the reason stated;
- tests under `tests/`;
- `docs/research/MR-009-proposal.md`.

## Do not touch

- the window regime, relevance rules, deduplication rules, or per-bucket caps;
- the job ledger's rules, the coverage cycle's behaviour for scheduled runs, the role stage;
- the `mr-v1` engine, the spec, `docs/DECISIONS.md`, `docs/planning/`, other workstreams' packets;
- the SQLite schema;
- frozen fixtures;
- GDELT code. The Actions-side GDELT check is a later, non-blocking item and is not part of this.

## Acceptance criteria

- [ ] the report states whether `--as-of` alone is safe for this, with the evidence;
- [ ] months 13–36 can be planned without the most recent 12, with no overlap and no gap, shown by
      tests;
- [ ] with the option unset, planned buckets and all existing test results are unchanged;
- [ ] the planner is still pure;
- [ ] every question in section 3 is answered from the code, with file references;
- [ ] a run with no paid Stage A/B/C analysis is shown to be possible, or the report says exactly
      what stands in the way;
- [ ] `docs/research/MR-009-proposal.md` contains all five sections;
- [ ] no network call, no real database access, no backfill run, no LLM call;
- [ ] focused tests, the full Python suite, `uv run ruff check .` and
      `uv run ruff format --check .` pass;
- [ ] no test makes a network call or reads the live database or `C:\Dev\MS-shared\`.

## Autonomy

Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Fix in-scope bugs you discover. Add
regression tests. Do not stop for cosmetic choices.

## Escalate only if

- skipping the stored months cannot be done without changing the window regime;
- a run with no paid analysis is impossible without changing ledger or coverage rules;
- the recommended place to run it would change the public/private security boundary.

Record the issue in the report and continue all unblocked work. Where it runs, what it spends and
the label budget are the closing proposal, not escalations.

## Git rules

Do not push, merge, rebase, reset, commit, stage, force checkout another worktree/branch, or modify
main. No Git writes of any kind.

## Completion report

Write `C:\Dev\MS-shared\reports\MR-009.md` from `docs/workstreams/REPORT_TEMPLATE.md`, and include:

1. **Outcome**: change needed or not, and the exact command
2. files changed
3. checks run, with real results
4. the items needing Alfred's approval: the change, where it runs, paid analysis, label budget
5. confirmation that nothing was fetched, written or run
6. assumptions still unvalidated — including that everything about Google's behaviour at this depth
   rests on MR-007's 13 probe requests
