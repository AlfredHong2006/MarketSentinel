# MarketSentinel Workstreams

Coordinator-owned control plane.  
Only Alfred (or the designated coordinator acting on Alfred's instruction) updates statuses here.

## Current milestone

**M8 — Historical Market Reaction (`mr-v1`)**

Goal: establish a leakage-safe, benchmark-adjusted historical market-reaction engine that becomes the quantitative foundation for later event-type impact and historical-analogue features.

## Active / queued work

| ID | Workstream | Owner | Status | Depends on | Worktree | Output |
|---|---|---|---|---|---|---|
| MR-001 | Data readiness | Claude Code A | DONE | none | `C:\Dev\MS-worktrees\data` (stale, removable) | readiness report + real fixture — in main as `b830183` |
| MR-002 | Quant core | Claude Code B | REVIEW | none | `C:\Dev\MS-worktrees\quant` | deterministic engine + tests — uncommitted in worktree, awaiting Alfred's commit/merge |
| MR-003 | Real-data validation | TBD | BLOCKED | MR-002 merged to main | `C:\Dev\MS-worktrees\validation` (create after merge) | frozen thresholds, placebo/lag validation, go/no-go — packet drafted |
| MR-004 | API + snapshot integration | TBD | BLOCKED | MR-003 contract freeze | created later | public reaction block + endpoint |
| MR-005 | Frontend | TBD | BLOCKED | MR-003 contract freeze | created later | Historical Market Reaction UI |

### Reconciliation notes (2026-10-06)

- **MR-001:** verdict READY WITH GAPS. Main's `b830183` has the same content as the data branch's
  `7508f52`; the data worktree holds nothing main lacks.
- **MR-002:** the post-review patch **was applied**. All four approved amendments (asymmetric
  thresholds, exact-only day 0, three-condition history sufficiency, `SPY` / `CUKX.L`) are in the
  worktree's engine, tests, spec, and `docs/DECISIONS.md`. No MR-002B packet is needed.
  Checked in the worktree on 2026-10-06: 87 focused tests pass, full suite 1036 pass, `ruff check`
  and `ruff format --check` clean. The MR-001 fixture is in the worktree's `tests/fixtures/` and is
  byte-identical to main's. No test reads `C:\Dev\MS-shared\`. No completion report exists (the
  September session predates the report rule).
- **MR-002 carries the methodology amendment.** The spec and `docs/DECISIONS.md` edits exist only in
  the quant worktree; main still holds the pre-review spec until that work is merged.
- **MR-003:** becomes READY the moment MR-002 is in main. Packet:
  `docs/workstreams/MR-003-real-data-validation.md`.

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
- Agents do not push, merge, rebase, reset, or modify main.
- Current policy: Alfred performs Git writes/commits.
- Revisit branch-local agent commits only if manual committing becomes a demonstrated bottleneck.

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
5. Alfred commits/integrates;
6. update this table;
7. launch newly unblocked work.
