# <WORKSTREAM ID> — <Name>

Status: READY  
Owner: <agent/session>  
Depends on: <IDs or none>  
Worktree: `<path>`

## Objective

One paragraph describing the finished outcome.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md` if present
- `<relevant product/spec doc>`

Do not reinterpret or redesign approved product/methodology decisions.

## Allowed scope

- `<paths/components this worker may change>`

## Do not touch

- `<explicit unrelated/high-conflict areas>`

## Required output / interface

Describe the artifact, data contract, module, report, endpoint or UI this workstream must produce.

## Acceptance criteria

- [ ] criterion 1
- [ ] criterion 2
- [ ] focused tests pass
- [ ] full applicable quality gate passes
- [ ] no real paid/network calls from tests

## Autonomy

Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Fix in-scope bugs you discover. Add regression tests. Do not stop for cosmetic choices.

## Escalate only if

- approved methodology must change;
- product semantics must change;
- public/private security boundary must change;
- paid-spend policy must change;
- persistent schema requires an incompatible decision;
- a blocker invalidates this workstream contract.

Continue all unblocked work before escalating.

## Git rules

Do not:
- push;
- merge;
- rebase;
- reset;
- force checkout another worktree/branch;
- modify main.

Current MarketSentinel policy: do not commit unless Alfred explicitly changes this policy.

## Completion report

Return only:

1. **Outcome**
2. **Files changed**
3. **Acceptance criteria / tests**
4. **Assumptions still unvalidated**
5. **Blocking issues**
6. **Nonblocking follow-ups**
