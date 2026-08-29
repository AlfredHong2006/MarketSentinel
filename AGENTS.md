# AGENTS.md

Working rules for AI coding agents in this repository. These are binding and sit alongside
[CLAUDE.md](CLAUDE.md), [docs/product/PRODUCT.md](docs/product/PRODUCT.md), and
[docs/decisions/DECISIONS.md](docs/decisions/DECISIONS.md).

## Inspect before editing

Read the module you are changing, its tests, and its callers before writing anything. Most rules
here are load-bearing and carry an explanatory comment stating why a threshold, pattern, or
ordering is what it is. Do not change a constant, regex, or sort key without first reading that
justification and the tests that pin it.

## Bounded scope only

Do exactly the requested slice, completely. Do not widen it, refactor adjacent code, rename things,
reformat untouched files, or fix unrelated problems you notice along the way — report them instead.
No unrelated cleanup.

## Follow the existing architecture

Work within the boundaries described in
[docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md). In particular: keep LLM
extraction separate from deterministic materiality; keep derived layers (materiality, grouping,
ranking, risks) unpersisted and recomputed; keep pure modules free of clocks, I/O, and randomness;
and keep evidence wording honest. Do not introduce a new architectural pattern, layer, dependency,
or persistence mechanism as a side effect of a bounded task.

## Do not invent consequential decisions

Product scope, methodology, evaluation policy, ranking philosophy, and subjective UX are the
product owner's decisions. You may analyse options and lay out trade-offs; you may not settle them
silently in code. Anything that changes what MarketSentinel *claims* — new thresholds, new gate
conditions, new displayed metrics, new user-facing wording about certainty or independence — is a
product decision, not an implementation detail.

Routine judgement calls inside an agreed slice are yours to make.

## Surface genuine ambiguity

When two readings of a task would produce materially different work, stop and ask the product
owner. Do everything that does not depend on the answer first, then ask one specific question.
Do not manufacture ambiguity for routine choices, and do not guess on consequential ones.

## Validation is required

Run the validation relevant to what you changed, and report the real result — including failures:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=marketsentinel --cov-report=term-missing
```

A change to selection, materiality, grouping, ranking, or the risk layer additionally requires:

```bash
uv run python scripts/evaluate_materiality.py evaluate --no-drift-check
```

Never relabel a gold fixture to make a metric improve. A negative result is a result; report it.

## Report back

Every completed task reports:

- **files changed** (and what each change does);
- **tests and validation run**, with actual outcomes;
- **deviations** from the request, and why;
- **blockers**, open questions, and anything left undone.

## Git: no AI writes

The product owner personally performs all repository-history changes.

**Read-only inspection is allowed**: `git status`, `git log`, `git diff`, `git show`, `git blame`,
`git ls-files`, `git branch --list`, `git remote -v`, `git stash list`.

**Prohibited**, without exception, even when asked to "just commit this":
`git add` / `git rm` / `git mv`, `git commit`, `git push`, `git pull`, `git fetch`, `git merge`,
`git rebase`, `git cherry-pick`, `git revert`, `git reset`, `git restore`, `git checkout` or
`git switch` that changes the working tree, `git branch` creation/deletion/renaming, `git tag`,
`git stash push`/`pop`/`apply`/`drop`, `git clean`, `git submodule update`, `git config`,
`git remote` changes, `git worktree` changes, `git gc`/`prune`/`reflog expire`, `git apply`,
`git am`, `git notes`, `git filter-branch`, and any `gh` command that creates or modifies a PR,
issue, release, or repository setting.

Leave changes in the working tree and tell the product owner what to review.

## No side effects without explicit instruction

Unless the task explicitly requires it, do not:

- **write to a database** — including `data/marketsentinel.db`. Tests use their own throwaway
  databases under `data/test-runtime/`; that is the only routine write path.
- **make network fetches** — no news providers, GDELT, Wikipedia constituents, yfinance, or model
  downloads. The test suite is offline and deterministic by design; keep it that way.
- **run backfills** — `scripts/backfill_historical_intelligence.py` fetches, scores, and analyses
  for real, in any mode.
- **make LLM calls** — Stage A/B/C and `scripts/smoke_event_intelligence.py` cost real money and
  mutate the stored analysis cache.

When one of these is genuinely required, say so and get explicit approval first, then use the
narrowest scope that does the job.

## Also do not

- Delete or overwrite the live database, its `.bak` snapshot, or `data/constituents_cache.json`.
- Read or echo secrets from `.env`.
- Bump a prompt or schema version constant casually — it invalidates the whole stored corpus.
- Create additional planning, status, or handover documents. The durable docs are CLAUDE.md,
  AGENTS.md, PRODUCT.md, DECISIONS.md, ARCHITECTURE.md, and README.md.
