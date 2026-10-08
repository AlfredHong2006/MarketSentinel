# MR-010 — Company-Role v2 Prompt and Ticker-Scoped Labelling

Status: DONE (merged to local main 2026-10-08; the re-pilot is an operational step)  
Owner: TBD (one worker, unattended run)  
Depends on: none  
Worktree: `C:\Dev\MS-worktrees\role-v2`

## Objective

Two changes to the company-role stage, built and tested offline with the provider mocked:

1. a new prompt version, `company-role-v2`, that fixes the three error patterns Alfred found in the
   v1 pilot;
2. role labelling limited to the tickers a dispatch names, so a run can never spend label budget on
   a company that was not asked for.

Nothing is spent. The re-pilot under v2 is a separate step Alfred dispatches after integration.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md`
- `docs/DECISIONS.md` — the 2026-10-08 entry "Label pilot failed (25 of 30); company-role-v2
  approved", the 2026-10-07 company-role and pilot-acceptance entries
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`, section 2.1
- `docs/planning/SCHEMA_6_ROLLOUT.md`, parts B and C
- `src/marketsentinel/company_role.py`, `company_role_ledger.py`, `coverage_cycle.py`,
  `scripts/run_coverage_cycle.py`, `.github/workflows/coverage.yml`
- `tests/test_company_role.py`, `tests/test_company_role_ledger.py`, `tests/test_company_role_cli.py`

Do not reinterpret or redesign approved product/methodology decisions.

## Hard limits

- No real LLM or API call. Every provider is a test double. Do not run
  `scripts/run_coverage_cycle.py`, `scripts/smoke_event_intelligence.py`, or anything else that can
  reach a provider.
- No API keys; do not read `.env`.
- No network.
- No database access except throwaway test databases under `data/test-runtime/`. Do not open
  `C:\Dev\MarketSentinel\data\marketsentinel.db`.
- No workflow dispatch, no paid run, no Git writes.

## What the pilot showed

Alfred reviewed 30 random v1 labels and judged 25 correct. The five errors fall into two patterns,
and a third change is his decision:

| pattern | v1 behaviour | v2 must say |
|---|---|---|
| the company is a counterparty in a transaction | the seller was labelled `mentioned` | a buyer **or seller** in a transaction is `principal` |
| an incident involving the company's own operations or assets | three of five reports of one such incident were labelled `mentioned`; also a case where the company was the actor | such an incident is `principal`, and so is any development in which the company is the actor |
| stock-price commentary, buy or sell opinions, analyst ratings and price targets | v1 instructs `principal` for a rating or price-target change about the company | these are `mentioned`: they are someone else's view of the company, not a development of the company |

The third row **reverses** a v1 instruction on purpose. Remove the v1 wording that makes the object
of an analyst rating or price-target change `principal`.

You do not have the pilot's headlines and must not ask for them or try to obtain them. The re-pilot
labels the same articles, so a prompt written around those specific headlines would make the review
meaningless. Write the rules as general definitions with invented illustrative shapes, not real
headlines, real company pairs, or real events.

## Work

### 1. `company-role-v2`

- Change `COMPANY_ROLE_PROMPT_VERSION` to `company-role-v2` and revise the instructions.
- Keep everything v1 gets right: the fenced, untrusted input; the fixed two-value vocabulary; the
  rule that grammatical position decides nothing; the existing `mentioned` shapes (a third party
  acting over the company's product, roundups, a competitor's news, comparison, supplier named in
  passing, employer in someone else's appointment); the rule not to label by sentiment or
  importance; the output fields and the rationale limit.
- Add the three rules above as definitions. Make the boundary between them explicit where they
  could collide. At minimum the prompt must decide these cases, each stated generally:
  - the company's shares fall or rise **because of** a development of the company: the development
    is what the article reports, so `principal`; an article that is only about the share-price move,
    a valuation view, or whether to buy or sell is `mentioned`;
  - an analyst rating or price-target change is `mentioned` even when the company is the only
    company named;
  - the company's own results, guidance, or announcement reported alongside analyst reaction is
    `principal`;
  - an incident at the company's own facility, fleet, network, or product in the company's own
    hands is `principal`; an incident at a customer or third party using the company's product
    stays `mentioned`.
- Do not add a third role, a confidence threshold, or any rule about sentiment.
- `COMPANY_ROLE_SCHEMA_VERSION` does not change unless the output schema changes, and it should
  not.
- State in the report the instruction length in characters and the change against v1, so the
  per-label input token count (measured for v1 at about 1,004) can be re-estimated.

### 2. v1 labels stay as history

- No stored v1 row is edited, deleted, or re-read as v2. The new contract key creates new ledger
  jobs; v1's `analyzed` jobs stay as they are.
- Tests: a database holding v1 labels and v1 jobs, after the version change, still holds them
  unchanged; every article gets a fresh `pending` v2 job; nothing reads a v1 label as a v2 label;
  with every cap at `0` nothing is spent.
- No schema change and no `user_version` change. If you find one is needed, stop and report it.

### 3. Ticker-scoped labelling

- The role stage labels only the tickers a run explicitly names for it. The dispatch's `tickers`
  input is that list; the workflow passes it through.
- Fail closed: a run with any positive role cap and no role ticker list is refused before any
  spend, with a clear message and a non-zero exit.
- An actively covered ticker that is not on the list gets no paid role call and consumes none of
  the role budget, in the new-article pass and in the backfill pass.
- A listed ticker that is not actively covered is reported plainly and labels nothing; it is not an
  error.
- The run's report states which tickers were in scope for labelling and which covered tickers were
  left out.
- A scheduled run has no dispatch input and so uses the input's default, `NVDA,PFE`. That is
  deliberate: a company activated by a public request gets no role labels until Alfred adds it.
  Say so in a comment where the default is defined.
- Stage A/B/C behaviour, the `--all-active` cycle, public-request admission and every existing cap
  are unchanged.
- Tests: three covered tickers with two listed, under a backfill cap and under new-article caps,
  label only the two and spend the budget only on them; the refusal with no list; the unlisted and
  the not-covered cases; the workflow passes the list to the cycle step; the Stage A/B/C ledger is
  untouched.

### 4. Documentation

- Update the company-role section of `docs/architecture/ARCHITECTURE.md` for the v2 version and the
  ticker scoping.
- Do not edit the spec. The coordinator applies the approved section 2.1 text at integration.

## Allowed scope

- `src/marketsentinel/company_role.py`, `company_role_ledger.py`;
- `src/marketsentinel/coverage_cycle.py` and `scripts/run_coverage_cycle.py`, only for the ticker
  scoping;
- `.github/workflows/coverage.yml`, only to pass the ticker list to the role stage;
- tests under `tests/`;
- `docs/architecture/ARCHITECTURE.md`.

## Do not touch

- Stage A/B/C prompts, their version constants, `analysis_compatibility.py`;
- the role vocabulary, the label table, the SQLite schema;
- the `mr-v1` engine and its role filter;
- the backfill script and its workflow step;
- `docs/product/`, `docs/DECISIONS.md`, `docs/planning/`, other workstreams' packets;
- frozen fixtures;
- the Pfizer Ltd (India) subsidiary case. It is a recorded caveat; do not add a rule for it.

## Acceptance criteria

- [ ] the prompt version is `company-role-v2`, the schema version is unchanged, and no Stage A/B/C
      constant changed;
- [ ] the v2 instructions state the three rules and decide the four boundary cases, and no longer
      make the object of an analyst rating or price-target change `principal`;
- [ ] the instructions contain no real headline, company pair, or event from the pilot;
- [ ] v1 labels and v1 jobs are untouched and never read as v2, shown by tests;
- [ ] the role stage labels only the listed tickers and refuses a positive cap with no list, shown
      by tests;
- [ ] the workflow passes the ticker list to the role stage and nothing else in it changed;
- [ ] with every role cap at `0` the stage spends nothing;
- [ ] no schema or `user_version` change;
- [ ] focused tests, the full Python suite, `uv run ruff check .` and
      `uv run ruff format --check .` pass;
- [ ] no test makes a network or provider call or reads the live database or `C:\Dev\MS-shared\`;
- [ ] no hard limit was crossed, and the report says so.

## Autonomy

This is an unattended run. Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Fix in-scope bugs you discover. Add
regression tests. Do not stop for cosmetic choices.

If a command you want is not pre-approved and would wait for a prompt, do not wait on it and do not
work around a denial: skip it and record it.

## Escalate only if

- the ticker scoping cannot be done without changing Stage A/B/C or ledger rules;
- v2 needs a schema change;
- two of Alfred's three rules cannot both hold for a case you can state generally. Report the case
  and the two readings; do not pick one silently.

Record the issue in the report and continue all unblocked work.

## Git rules

Do not push, merge, rebase, reset, commit, stage, force checkout another worktree/branch, or modify
main. No Git writes of any kind.

## Completion report

Write `C:\Dev\MS-shared\reports\MR-010.md` from `docs/workstreams/REPORT_TEMPLATE.md`, and include:

1. **Outcome**
2. the full v2 instructions, quoted, and a short list of what changed against v1
3. how the ticker scoping behaves in each case
4. files changed
5. checks run, with real results
6. confirmation that no hard limit was crossed
7. assumptions still unvalidated — first among them that mocked tests say nothing about how a real
   model applies the v2 wording; the re-pilot is the only evidence
8. decisions needed from Alfred, or "None"
