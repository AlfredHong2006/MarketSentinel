# MR-002 — Historical Market Reaction Quant Core

Status: READY  
Owner: Claude Code B  
Depends on: none initially; consume MR-001 real fixture as soon as it is available  
Worktree: `C:\Dev\MS-worktrees\quant`

## Objective

Implement the pure deterministic `mr-v1` historical-market-reaction engine and comprehensive offline tests.

The engine must be independent of frontend/API/snapshot concerns and must make no paid LLM calls.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md` if present
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`

Do not redesign the methodology.

If the spec is internally contradictory, document the contradiction and continue all unblocked implementation before escalating.

## Core capabilities

Implement deterministic logic for:

1. article polarity: `p_positive - p_negative`;
2. canonical + within-session normalized-title dedup integration;
3. exchange-aware assignment to first session close after publication;
4. US and London calendars, holidays, early closes, DST;
5. conservative date-only timestamp handling;
6. session-sentiment aggregation;
7. distinct-source and article-ID provenance;
8. positive/negative regime qualification;
9. adjusted stock and benchmark return alignment;
10. +5 primary market-adjusted return;
11. day-0 through +10 reaction path;
12. same-regime +5 event-window exclusivity;
13. mean, median, n and outperform-market share;
14. deterministic percentile-bootstrap CI;
15. split-half stability;
16. mean/median sign-disagreement -> unstable/outlier-sensitive;
17. evidence/data-quality states;
18. extreme-return flagging without automatic deletion;
19. versioned result object suitable for later snapshot/API integration.

Do not freeze the numeric `tau` from return outcomes. Support the selection/frozen-constant interface required by the spec.

## Required tests

At minimum:

- [ ] during-session article assignment;
- [ ] after-close -> next session;
- [ ] weekend -> next session;
- [ ] US holiday;
- [ ] London holiday/early-close case;
- [ ] US/UK DST-divergence period;
- [ ] date-only publication rule;
- [ ] deterministic title dedup;
- [ ] no-news session is missing, not zero;
- [ ] adjusted-return arithmetic;
- [ ] benchmark-adjusted return arithmetic;
- [ ] missing stock/benchmark data;
- [ ] complete-path eligibility;
- [ ] event-window exclusivity;
- [ ] deterministic bootstrap;
- [ ] insufficient/preliminary/detected/no-relationship/unstable states;
- [ ] split-half failure;
- [ ] mean/median sign disagreement;
- [ ] extreme valid move retained but flagged;
- [ ] synthetic null/placebo behavior;
- [ ] methodology version carried through result;
- [ ] article provenance preserved.

## Real-fixture handoff

Start with synthetic fixtures.

When MR-001 publishes its sanitized real fixture, consume it in a small offline integration/regression test.

Do not wait idly for MR-001 if synthetic implementation can proceed.

If the real fixture exposes a code-level bug, fix it. If it exposes a methodology decision, record the issue and do not silently change `mr-v1`.

## Allowed scope

May add/modify:
- a narrowly scoped market-reaction domain/module under `src/marketsentinel/`;
- corresponding tests/fixtures;
- a minimal dependency required for exchange calendars if the repo lacks one;
- domain result models that are internal to the pure engine.

Keep changes isolated from API/frontend/snapshot integration.

## Do not touch

- React frontend;
- public API endpoints;
- public snapshot publication;
- R2/request system;
- paid coverage budgets;
- LLM prompts/contracts;
- unrelated ingestion behavior.

## Quality gates

Run:
- focused market-reaction tests;
- full Python suite;
- ruff check;
- ruff format check.

Tests must not make real paid/network calls.

## Escalate only if

- adjusted-price semantics make the approved return definition impossible;
- no viable exchange-calendar mechanism exists in the current environment;
- the approved methodology is contradictory;
- a persistent-schema change is unavoidable;
- public/private or paid-spend behavior would have to change.

Continue everything else before escalating.

## Git rules

No push, merge, rebase, reset, or main modification.  
Current policy: no commits; Alfred performs Git writes.

## Completion report

Return:

1. **Outcome**
2. **Exact mathematical implementation**
3. **Files changed**
4. **Tests/results**
5. **Real-fixture findings**
6. **Assumptions still unvalidated**
7. **Blocking issues**
8. **Nonblocking follow-ups**
