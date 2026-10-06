# MarketSentinel Operating Model

Solo-founder development setup. Adopted 2026-10-06. Change it only when something in it becomes a demonstrated pain point.

**Files remember. Agents execute. `/coordinate` reconciles. Alfred decides.**

## Focus

- MarketSentinel is the only active product.
- HealthTrend finishes its current work, then goes to maintenance only.
- Remaining time goes to internships and C++. No new side projects.

## Roles

| Who | Does | Does not |
|---|---|---|
| Alfred | Product calls, approves each integration, pushes, deploys, go/no-go | Relay terminal output between chats, schedule routine work |
| Worker (Claude Code, one per worktree) | Executes one packet to DONE or BLOCKED, writes a report | Edit `docs/planning/`, change methodology, any Git writes |
| `/coordinate` (fresh Claude Code session in main) | Reconciles disk state, updates `WORKSTREAMS.md`, drafts next packet, lists decisions; after Alfred approves an integration, performs its local Git operations | Feature code, methodology, push, force-push, rebase, `reset --hard`, deploy |
| Fable / strong chat | Methodology and architecture gates only | Routine implementation or review |
| ChatGPT / Claude chat | Second opinion, product thinking, challenging the lead engineer | Routing work |

## Concurrency

- At most 2 MarketSentinel workers at once.
- At most 3 coding agents across all projects while HealthTrend is still active; 2 after it stops.

## Repository layout

```text
C:\Dev\MarketSentinel\                 main = headquarters, always clean and deployable
  .claude\commands\coordinate.md       the coordinator command
  .claude\settings.json                grants read access to MS-worktrees and MS-shared
  docs\product\HISTORICAL_MARKET_REACTION_V1.md   frozen mr-v1 spec
  docs\planning\WORKSTREAMS.md         live control plane (main's copy only)
  docs\planning\OPERATING_MODEL.md     this file
  docs\workstreams\MR-XXX-*.md         one packet per workstream
  docs\workstreams\REPORT_TEMPLATE.md  required worker report format
  docs\DECISIONS.md                    append-only, one dated paragraph per decision

C:\Dev\MS-worktrees\<workstream>\      one per active worker, deleted after merge

C:\Dev\MS-shared\
  reports\MR-XXX.md                    worker completion reports
  fixtures\                            temporary cross-worktree handoff only
```

## Rules

1. Main's copy of `docs/planning/` is the only live one. Copies inside worktrees are ignored.
2. Workers never edit `docs/planning/` or another workstream's packet.
3. A worker writes any shared fixture first and its report last. If the report exists, the handoff is complete.
4. Anything a test depends on must be copied into `tests/fixtures/` in that branch. CI cannot see `MS-shared`.
5. CI is the definition of done: pytest, ruff check, ruff format --check, plus frontend typecheck/lint/build where touched.
6. mr-v1 methodology is immutable after freeze. After freeze, a material change means mr-v2 and an entry in `DECISIONS.md`. Before freeze, a change still needs Alfred's approval and a `DECISIONS.md` entry.
7. Statuses are READY, RUNNING, BLOCKED, REVIEW, DONE. A workstream with a report but unmet acceptance criteria stays REVIEW.

## Worker autonomy

Workers run until acceptance criteria pass. They decide naming, refactors, in-scope bug fixes and implementation details themselves.

They escalate (record in report, continue on anything unblocked) only for: methodology, product semantics, public/private security boundary, paid-spend policy, persistent schema, or a blocker that invalidates the packet.

## Integration policy

Adopted 2026-10-06; see `docs/DECISIONS.md`.

- The coordinator may perform local Git operations — commit, merge into local `main`, create and remove worktrees and branches — after Alfred approves each integration. Approval is per integration and does not carry over.
- No agent ever pushes, force-pushes, rebases, runs `reset --hard`, or deploys. Those stay with Alfred.
- Workers perform no Git writes.
- Main stays clean and deployable: the coordinator runs the full check set on the merged result before removing any worktree or branch.

## Integration window (every day or two, ~30 minutes)

1. Open a fresh Claude Code session in main and run `/coordinate`.
2. For each branch it lists as ready: read "Assumptions" and "Decisions needed" in the report, spot-check two acceptance criteria, then approve or decline that integration.
3. For each approved integration the coordinator commits and merges in dependency order, runs the checks on main, and commits its own doc changes.
4. The coordinator deletes merged worktrees and creates worktrees for newly unblocked packets. Alfred starts those workers.
5. Alfred pushes and deploys.
6. Bring decisions to a chat only if you want a second opinion.

## Starting a worker

```powershell
git -C C:\Dev\MarketSentinel worktree add C:\Dev\MS-worktrees\<name> -b ms/<id>-<name>
```

Open Claude Code in that folder and send:

> Read `docs/workstreams/<packet>.md` and `docs/product/HISTORICAL_MARKET_REACTION_V1.md`. Execute the packet autonomously until DONE or BLOCKED. Write your report to `C:\Dev\MS-shared\reports\<ID>.md` using `docs/workstreams/REPORT_TEMPLATE.md`. Do not edit `docs/planning/`. No Git writes.

## Deliberately deferred

Add these only when their absence becomes a real problem: launcher scripts or headless workers, branch-local agent commits, reviewer agents on every stream (one reviewer for MR-003 only), SHA manifests and atomic file handoff, auto-merge.

## Current state (2026-10-06)

- MR-001 data readiness: in main.
- MR-002 quant core: in main, including the four amendments the principal methodology review approved (asymmetric positive/negative thresholds; exact-only day-0 aggregation; density-aware history sufficiency; SPY / CUKX.L benchmark mapping). The amended spec is ratified as mr-v1.
- Next: MR-003 real-data validation on NVDA and PFE (sequential; go/no-go is Alfred's).
- Then MR-004 API/snapshot and MR-005 frontend in parallel once MR-003 freezes the result contract.

`docs/planning/WORKSTREAMS.md` is the live status; this section is only a snapshot.

Old September worker sessions are not retrofitted with the new reporting rules. This model applies from the next workstream onward.

## First coordinator run

After setup, in a fresh session in main, run `/coordinate` and add:

> MR-001 and the initial MR-002 implementation completed in September. A principal methodology review then approved four changes: asymmetric positive/negative thresholds, exact-only day-0 aggregation, density-aware history sufficiency, and SPY/CUKX.L benchmark mapping. Reconcile main and all worktrees, determine whether the MR-002 post-review patch was actually applied, and check whether the MR-001 fixture is in `tests/fixtures/` on the quant branch. Do not assume September statuses are current. Update `WORKSTREAMS.md` and draft either MR-002B or MR-003.

## One-time setup

Save `coordinate.md`, `REPORT_TEMPLATE.md` and this file to Downloads, then:

```powershell
$repo = "C:\Dev\MarketSentinel"
$dl   = "$env:USERPROFILE\Downloads"

New-Item -ItemType Directory -Force "$repo\.claude\commands", "$repo\docs\workstreams", "$repo\docs\planning" | Out-Null
New-Item -ItemType Directory -Force "C:\Dev\MS-shared\reports", "C:\Dev\MS-shared\fixtures" | Out-Null

Copy-Item "$dl\coordinate.md"        "$repo\.claude\commands\coordinate.md"
Copy-Item "$dl\REPORT_TEMPLATE.md"   "$repo\docs\workstreams\REPORT_TEMPLATE.md"
Copy-Item "$dl\OPERATING_MODEL.md"   "$repo\docs\planning\OPERATING_MODEL.md"

$settings = "$repo\.claude\settings.json"
if (-not (Test-Path $settings)) {
@'
{
  "permissions": {
    "additionalDirectories": ["C:\\Dev\\MS-worktrees", "C:\\Dev\\MS-shared"]
  }
}
'@ | Set-Content $settings -Encoding utf8
  Write-Host "Created settings.json"
} else {
  Write-Host "settings.json exists: add C:\\Dev\\MS-worktrees and C:\\Dev\\MS-shared to permissions.additionalDirectories by hand"
}

git -C $repo status --short
```

Then commit these files to main yourself.
