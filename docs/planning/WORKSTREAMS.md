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
| MR-003 | Real-data validation | TBD | READY | none outstanding | `C:\Dev\MS-worktrees\validation` (not yet created) | frozen thresholds, placebo/lag validation on NVDA/PFE, go/no-go |
| MR-004 | API + snapshot integration | TBD | BLOCKED | MR-003 contract freeze | created later | public reaction block + endpoint; must settle price persistence |
| MR-005 | Frontend | TBD | BLOCKED | MR-003 contract freeze | created later | Historical Market Reaction UI |

### Reconciliation notes (2026-10-06)

- **MR-001:** verdict READY WITH GAPS. In main as `b830183`. Worktree and branch removed.
- **MR-002:** merged to local main, including all four approved amendments (asymmetric thresholds,
  exact-only day 0, three-condition history sufficiency, `SPY` / `CUKX.L`) in the engine, tests,
  spec, and `docs/DECISIONS.md`. Checked on merged main with the CI install
  (`uv sync --locked --all-extras --dev`): 1045 tests pass, `ruff check` and `ruff format --check`
  clean. Worktree removed; branch `work/mr-002-quant-core` kept. No completion report exists (the
  September session predates the report rule).
- **Spec:** the amended spec is ratified as `mr-v1` (`docs/DECISIONS.md`, 2026-10-06).
- **MR-003:** READY. Packet: `docs/workstreams/MR-003-real-data-validation.md`. Scope is NVDA and PFE
  only; the London (LSE) path is untested in validation; backfill is decided after its result.
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
