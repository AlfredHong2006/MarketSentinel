# MR-006 — Primary-Company Pre-Analysis

Status: REVIEW (offline work complete 2026-10-07, acceptance criteria met; awaiting Alfred's approval to integrate)  
Owner: TBD (one worker, unattended overnight run)  
Depends on: none  
Worktree: `C:\Dev\MS-worktrees\primary-company`

## Objective

Implement, fully offline, the ability to record for each stored article whether the covered company
is the **principal subject** of the article or is merely **mentioned**, wire it into the existing
scheduled analysis worker behind fixed budgets, and make the `mr-v1` engine able to filter on it.

Everything is built and tested with the LLM mocked. Nothing is spent, no real API is called, and no
real database is written. The workstream ends with a written proposal — budget, schema change,
filter rule, and spec amendment — for Alfred's approval. The real backfill and the first paid run
happen after that approval, in a separate step that is not part of this packet.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md`
- `docs/DECISIONS.md` — especially the three 2026-10-06 entries after MR-003, the 2026-09-13
  entries on the analysis job ledger and scheduled coverage, and the 2026-08-27 entry on extraction
  versus materiality
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`
- `docs/architecture/ARCHITECTURE.md`
- `C:\Dev\MS-shared\reports\MR-003.md`, section F1 (why no existing field or title rule works)
- `src/marketsentinel/event_analysis.py`, `analysis_ledger.py`, `coverage_cycle.py`,
  `analysis_compatibility.py`, `subject_principal.py`, `storage/sqlite.py`,
  `src/marketsentinel/market_reaction/`, `.github/workflows/coverage.yml`

Do not reinterpret or redesign approved product/methodology decisions.

## Hard limits for this run

These hold for the entire workstream, with no exceptions:

- **No real LLM or API call.** Every provider is a test double. Do not run
  `scripts/smoke_event_intelligence.py`, `scripts/run_coverage_cycle.py`,
  `scripts/backfill_historical_intelligence.py`, or anything else that can reach a provider.
- **No API keys.** Do not read `.env`, do not set or echo any key, do not add a key to any file.
- **No database writes.** Do not open `C:\Dev\MarketSentinel\data\marketsentinel.db` for writing,
  and do not copy it. Tests use throwaway databases under `data/test-runtime/` only. If corpus
  counts are needed for the budget, read that database strictly read-only (`mode=ro`).
- **No network.** No news providers, GDELT, yfinance, Wikipedia, model downloads, or R2.
- **No paid runs** and no workflow dispatch.
- **No Git writes of any kind.**

If a step cannot be done inside these limits, do not do it. Record it in the report as work for the
post-approval step.

## Fixed design constraints

1. **A separate stage, not a Stage A change.** Primary-company extraction is its own stage with its
   own prompt version, schema version, and stored rows. Do **not** change
   `STAGE_A_PROMPT_VERSION`, `STAGE_B_PROMPT_VERSION`, `STAGE_C_PROMPT_VERSION`, or
   `ARTICLE_ANALYSIS_SCHEMA_VERSION`, and do not alter the exact-equality rule in
   `analysis_compatibility.py`. Existing stored analyses must stay valid for display and reuse.
2. **Extraction and the rule that uses it stay separate** (invariant 1). The model records the
   company's role in the article. Whether an article counts toward a session signal is a
   deterministic, auditable rule in code, never in a prompt.
3. **Stored, versioned, immutable** (invariant 3). A label is keyed by article fingerprint plus the
   stage's own version fields and is never overwritten. A version change means new rows, not edits.
4. **Article text is untrusted data** (invariant 4). Fence it in the prompt, never follow it as
   instruction, and validate provider output structurally and semantically: the role must come from
   the fixed vocabulary and the company must be the one the application supplied.
5. **Failure is safe** (invariant 7). An article that could not be labelled gets a typed status
   (`unavailable` / `failed` / `not_found`). No role is ever guessed or defaulted. An unlabelled
   article must be distinguishable from one labelled "mentioned".
6. **Spend only through the ledger, in the private worker.** Reuse the job-ledger mechanics
   (leases, retries, skip rule, ordering) so each article is paid for once per contract. The public
   deployment gains no credential and no spend path.
7. **Budgets are fixed, explicit, and default to zero.** A per-run cap for new articles and a
   separate total cap for the one-off backfill, exposed as `workflow_dispatch` inputs like the
   existing ones. As delivered, the defaults must make the scheduled workflow spend **nothing** on
   this stage until Alfred sets them. Budget-limited work stays pending; it is never marked done.
8. **Do not touch `subject_principal.py`'s rule or the materiality layer.** This stage is a new
   input to `mr-v1` only. Whether materiality or risks should ever read it is not this packet's
   question.
9. **The engine stays pure.** `market_reaction` takes the labels as input data; it performs no I/O,
   reads no clock, and makes no call.

## Work

### 1. Role extraction stage

- A small role vocabulary. At minimum `principal` and `mentioned`; add a third value such as
  `counterparty` only if the MR-003 failure shapes need it, and justify it in the report.
- Prompt, response schema, validation, and versioning for the stage.
- A provider interface with a deterministic test double. Tests cover: the MR-003 failure shapes
  (a third party reprimanded over the company's product; a third party charged over the company's
  chips; a roundup naming the company in passing; a competitor's approval), a clean principal case,
  malformed output, an out-of-vocabulary role, a wrong company, prompt-injection text inside an
  article, and provider failure.

### 2. Persistence

- Storage for the labels in `storage/sqlite.py`, with the migration and the `PRAGMA user_version`
  bump implemented and tested against throwaway databases.
- The schema change is **proposed, not settled**: it is listed for Alfred's approval in the closing
  proposal. State exactly what the migration does to an existing version-5 database.
- Analyse and report the deployment interaction: the public service checks the snapshot's
  `schema_user_version` against its own build and falls back to the baked-in snapshot on mismatch.
  State the safe order of operations for rolling out a version bump, and whether the label table
  belongs in the public snapshot at all.

### 3. Scheduled worker integration

- Extend the coverage cycle and `.github/workflows/coverage.yml` so the stage runs for new articles
  and, separately, for the one-off backfill of stored articles, each under its own cap.
- Ordering must be deterministic and must put the articles that can change an `mr-v1` result first
  (articles in sessions with `>= 3` distinct sources), so a partial backfill is still useful.
- The existing checkpoint ordering must hold: paid results are checkpointed to R2 before any public
  snapshot work.
- Tests cover: caps respected, budget-limited articles stay pending, an article is never paid for
  twice, a failed call retries per the ledger rules, and zero-default budgets spend nothing.

### 4. Engine filter

- Let the engine accept per-article role labels and apply a deterministic eligibility rule.
- Implement **both** candidate placements behind an explicit, tested switch, and do not choose
  between them:
  - (a) filter articles before session signals are built, which also changes the threshold
    selection population `E`;
  - (b) keep signals as they are and filter at event qualification.
- Decide and test how an **unlabelled** article is treated under each placement, and present the
  options (exclude, include, or make the session ineligible) in the proposal. Silently treating
  unlabelled as principal or as mentioned is not acceptable.
- With no labels supplied, the engine's behaviour and every existing test result must be unchanged.
- The result object must report how many articles were excluded by the rule and how many were
  unlabelled, so a consumer can see the filter's effect.
- Do not set `MR_V1_FROZEN_THRESHOLDS`. Do not compute or inspect any return outcome on real data.

### 5. Closing proposal

Write `docs/research/MR-006-proposal.md` containing, for Alfred's approval:

1. **Budget.** Article counts from the read-only corpus (total, and the `>= 3`-source subset), the
   tokens per call implied by the actual prompt, and the resulting backfill size and steady-state
   per-run volume, as token counts. Give recommended per-run and backfill caps. Do not fetch or
   quote live prices; state the cost formula and leave the price per token as an input.
2. **Schema change.** The exact table, the `user_version` bump, the migration, and the rollout order.
3. **Filter rule.** Placement (a) versus (b), the unlabelled-article options, and a recommendation
   with reasons, including what each does to outcome-blind threshold selection.
4. **Spec amendment.** The exact proposed text changes to
   `docs/product/HISTORICAL_MARKET_REACTION_V1.md`, as a diff-style block in the proposal. The spec
   currently says `mr-v1` uses the broader sentiment-scored corpus and does not require paid LLM
   analysis; say plainly how the amendment changes that. Do not edit the spec itself.
5. **Post-approval runbook.** The exact steps, in order, to run the one-off backfill and enable the
   steady-state stage, including what to check before and after.

## Allowed scope

- a new module (or small set of modules) under `src/marketsentinel/` for the role stage;
- `src/marketsentinel/domain.py` for the new contracts;
- `src/marketsentinel/storage/sqlite.py` for the new table and migration;
- `src/marketsentinel/analysis_ledger.py` and `coverage_cycle.py`, only as far as the new stage needs;
- `src/marketsentinel/config.py` for new settings;
- `src/marketsentinel/market_reaction/` for the filter;
- `scripts/run_coverage_cycle.py` and `.github/workflows/coverage.yml` for the new caps;
- tests and fixtures under `tests/`;
- `docs/research/MR-006-proposal.md`;
- `docs/architecture/ARCHITECTURE.md`, to describe the new stage once it exists.

## Do not touch

- Stage A/B/C prompts, their version constants, and `analysis_compatibility.py`;
- `event_policy.py`, `materiality.py`, `subject_principal.py`, `risk_*.py`;
- the evidence window and evidence ranking;
- frozen fixtures in `tests/fixtures/`;
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`, `docs/DECISIONS.md`, `docs/planning/`, and other
  workstreams' packets;
- the public API, the dashboard, the frontend, and the public-request system;
- the public/private R2 security model;
- the `validation` worktree and anything in it.

## Acceptance criteria

- [ ] the role stage exists with its own prompt and schema versions, and no Stage A/B/C version
      constant or the compatibility rule changed;
- [ ] provider output is validated structurally and semantically; the MR-003 failure shapes and the
      injection, malformed, wrong-company, and failure cases are tested;
- [ ] an unlabelled article is always distinguishable from a "mentioned" one, in storage and in the
      engine;
- [ ] labels are stored versioned and immutable; the migration is tested on a throwaway version-5
      database;
- [ ] the stage runs through the ledger under per-run and backfill caps whose delivered defaults
      spend nothing;
- [ ] backfill ordering is deterministic and puts `>= 3`-source-session articles first;
- [ ] both filter placements are implemented behind a tested switch, with no labels leaving every
      existing engine result unchanged;
- [ ] the result reports excluded and unlabelled article counts;
- [ ] `MR_V1_FROZEN_THRESHOLDS` is still unset;
- [ ] `docs/research/MR-006-proposal.md` contains all five sections;
- [ ] no test makes a network or provider call, reads the live database, or reads
      `C:\Dev\MS-shared\`;
- [ ] focused tests, the full Python suite, `uv run ruff check .`, and
      `uv run ruff format --check .` pass;
- [ ] `uv run python scripts/evaluate_materiality.py evaluate --no-drift-check` gives the same
      score as on main, showing materiality was not disturbed;
- [ ] none of the hard limits was crossed, and the report says so explicitly.

## Autonomy

This is an unattended overnight run. Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Fix in-scope bugs you discover. Add
regression tests. Do not stop for cosmetic choices.

If a command you want is not pre-approved and would wait for a prompt, do not wait on it: find a
pre-approved way, or skip it and record it. Never work around a denied permission.

## Escalate only if

- the stage cannot be built without changing a Stage A/B/C version constant or the compatibility
  rule;
- the public/private security boundary would have to change;
- the label cannot be stored without an incompatible change to an existing table;
- a hard limit above would have to be crossed to finish.

Record the issue in the report and continue all unblocked work. Budget, schema, filter rule, and
spec wording are not escalations: they are the closing proposal.

## Git rules

Do not push, merge, rebase, reset, commit, stage, force checkout another worktree/branch, or modify
main. No Git writes of any kind.

## Completion report

Write `C:\Dev\MS-shared\reports\MR-006.md` from `docs/workstreams/REPORT_TEMPLATE.md`, and include:

1. **Outcome**
2. files changed
3. checks run, with real results
4. confirmation that no hard limit was crossed
5. the four items needing Alfred's approval, each with the recommendation, pointing to
   `docs/research/MR-006-proposal.md`
6. assumptions still unvalidated — including that the mocked provider says nothing about real label
   quality
7. nonblocking follow-ups
