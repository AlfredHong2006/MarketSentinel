# Schema-6 Rollout and Role-Label Pilot Checklist

Operational checklist for Alfred. Every step here is his: it pushes, deploys, or dispatches a paid
workflow. Decisions behind it: `docs/DECISIONS.md`, 2026-10-07. Background and reasoning:
`docs/research/MR-006-proposal.md`, sections 2 and 5.

Commands are PowerShell, run from `C:\Dev\MarketSentinel`. They use the GitHub CLI (`gh`) and the
AWS CLI (`aws`). **Neither was found on this machine's PATH on 2026-10-07**; install them
(`winget install GitHub.cli`, `winget install Amazon.AWSCLI`, then `gh auth login`) or do the
marked steps in the GitHub, Cloudflare and Render dashboards instead.

Fill these in once per session. The values are in the GitHub Actions secrets and the Render
service; they are not in the repository.

```powershell
$ENDPOINT = "https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com"
$PRIVATE  = "s3://<R2_PRIVATE_BUCKET>"
$PUBLIC   = "<R2_PUBLIC_BASE_URL>"          # no trailing slash
$SITE     = "<public API base URL on Render>"
$env:AWS_ACCESS_KEY_ID     = "<R2_ACCESS_KEY_ID>"
$env:AWS_SECRET_ACCESS_KEY = "<R2_SECRET_ACCESS_KEY>"
$env:AWS_DEFAULT_REGION    = "auto"
```

## Why the order matters

The public service applies a published snapshot only when the snapshot's schema version equals its
own build's. On a mismatch it does not fail; it silently serves the snapshot baked into its image.
So the public build must be on schema 6 **before** the worker publishes a schema-6 snapshot, and
the baked snapshot should be recent in case it is served for a while.

## Part A — Schema rollout (no role spend)

### 1. Pre-flight on local main

```powershell
git status --short
git log --oneline origin/main..main
uv sync --locked --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/evaluate_materiality.py evaluate --no-drift-check
```

Check: only `docs/Private/` is untracked; tests pass; ruff clean; evaluation prints `PASS`.

### 2. Wait for any running coverage job, then disable the schedule

```powershell
gh run list --workflow coverage.yml --limit 3
gh workflow disable coverage.yml
gh workflow view coverage.yml
```

Check: no run is `in_progress` or `queued` before you disable; the workflow then shows as disabled.
The cron fires at 00:00, 06:00, 12:00 and 18:00 UTC, so start well clear of those.

### 3. Back up the private database in R2

```powershell
$STAMP = Get-Date -Format "yyyyMMdd-HHmm"
aws s3 cp "$PRIVATE/state/marketsentinel.db" "$PRIVATE/backups/marketsentinel-v5-$STAMP.db" --endpoint-url $ENDPOINT
aws s3 ls "$PRIVATE/state/" --endpoint-url $ENDPOINT
aws s3 ls "$PRIVATE/backups/" --endpoint-url $ENDPOINT
```

Check: the backup's size equals `state/marketsentinel.db`'s. Keep `$STAMP`; rollback needs it.

### 4. Refresh the baked fallback snapshot

The committed `deploy/public-snapshot.db` dates from early September, and
`scripts/build_deployment_snapshot.py` builds from the local `data/marketsentinel.db`, which was
last written on 2026-09-13. Bring the local copy up to date first, keeping the old one.

```powershell
Copy-Item data\marketsentinel.db data\marketsentinel.local-backup.db
Copy-Item data\constituents_cache.json data\constituents_cache.local-backup.json
aws s3 cp "$PRIVATE/state/marketsentinel.db" data\marketsentinel.db --endpoint-url $ENDPOINT
aws s3 cp "$PRIVATE/state/constituents_cache.json" data\constituents_cache.json --endpoint-url $ENDPOINT
uv run python scripts/check_database_integrity.py data/marketsentinel.db
uv run python scripts/build_deployment_snapshot.py
git status --short deploy
```

Check: the integrity check passes; the build ends with `RESULT: CLEAN`. If it prints
`REVIEW REQUIRED -- do not commit`, stop.

Do not start the local API or run a coverage cycle between the download and the build: either
would write to the copy you just downloaded.

```powershell
git add deploy/public-snapshot.db deploy/constituents_cache.json
git commit -m "chore: refresh baked public snapshot before schema 6 rollout"
```

The baked file is still schema 5. That is expected: the public service runs the schema script at
startup, which adds the empty label table in place.

### 5. Push

```powershell
git push origin main
gh run list --workflow ci.yml --limit 1
gh run watch
```

Check: CI is green on the pushed commit before going further.

### 6. Deploy the public build

In the Render dashboard: the MarketSentinel service → **Manual Deploy** → **Deploy latest commit**.

```powershell
Invoke-RestMethod "$SITE/health"
Invoke-RestMethod "$SITE/api/v1/capabilities"
```

Check: the deploy is live on the pushed commit and both calls answer. The Render log should show
`public snapshot: not applied (...) -- keeping the image's baked-in snapshot`. That is the expected
degraded state: the last published manifest is still schema 5.

### 7. Re-enable the schedule and dispatch one run with every role cap at zero

A disabled workflow cannot be dispatched, so enable it first. From here any run, scheduled or
manual, is a schema-6 worker with role caps at zero.

```powershell
gh workflow enable coverage.yml
gh workflow run coverage.yml -f max_new_roles=0 -f max_new_roles_total=0 -f max_backfill_roles=0
gh run list --workflow coverage.yml --limit 1
gh run watch
```

This run spends nothing on role labels. It is otherwise a normal coverage run and pays for
Stage A/B/C analyses under the usual caps, exactly as a scheduled run does.

Check in the run log:
- the cycle step prints no positive `role stage:` spend;
- both integrity checks pass and the checkpoint to R2 succeeds;
- the snapshot is built, published, and the Render restart step succeeds.

### 8. Verify schema 6 end to end

```powershell
$m = Invoke-RestMethod "$PUBLIC/latest.json"
$m.schema_user_version
$m.version
New-Item -ItemType Directory -Force "$env:TEMP\ms-rollout" | Out-Null
Invoke-WebRequest $m.database.url -OutFile "$env:TEMP\ms-rollout\snapshot.db"
@'
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print("user_version:", db.execute("PRAGMA user_version").fetchone()[0])
print("role labels:", db.execute("SELECT COUNT(*) FROM article_company_roles").fetchone()[0])
print("articles:", db.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
print("analyses:", db.execute("SELECT COUNT(*) FROM article_intelligence_analyses").fetchone()[0])
'@ | uv run python - "$env:TEMP\ms-rollout\snapshot.db"
Invoke-RestMethod "$SITE/api/v1/capabilities"
```

Check: `schema_user_version` is `6`; `user_version: 6`; `role labels: 0`; the article and analysis
counts are at least what they were before. The Render log now shows
`public snapshot: applied version=<the version above>`.

If the Render log still says `not applied`, the public build is not on the pushed commit: redo
step 6, then restart the service.

### Rollback for Part A

```powershell
gh workflow disable coverage.yml
aws s3 cp "$PRIVATE/backups/marketsentinel-v5-$STAMP.db" "$PRIVATE/state/marketsentinel.db" --endpoint-url $ENDPOINT
```

Then redeploy the previous commit in Render. A schema-5 build that opens a schema-6 file ignores
the extra table. Restore your local copy with
`Copy-Item data\marketsentinel.local-backup.db data\marketsentinel.db -Force` if you want the old
local state back.

## Part B — Pilot of 200 labels (first role spend)

Do not start Part B until every check in step 8 passed.

### 9. Fix the review sample's seed before the pilot

The seed is **20261007**. It is written here, before any label exists, so the 30 labels reviewed
cannot be picked after seeing them.

### 10. Dispatch the pilot

```powershell
gh run list --workflow coverage.yml --limit 3
gh workflow run coverage.yml -f tickers=NVDA,PFE -f max_new_roles=0 -f max_new_roles_total=0 -f max_backfill_roles=200
gh run watch
```

Check in the run log:
- `role stage:` shows `paid_attempts=200` and `stopped=budget`;
- `role ledger:` shows `analyzed=200`, with cumulative `input_tokens` and `output_tokens`;
- tokens per call are near the plan of about 900 in and 70 out. If input is above roughly 1,375 per
  call (25% over the 1,100 ceiling), stop and revisit the budget;
- no `circuit_breaker` and no `provider_unavailable`;
- the job finished well inside its 90-minute limit. Note the seconds per call.

Expected cost at $0.15 per 1M input and $0.60 per 1M output tokens: about $0.04.

If public coverage requests have activated other companies, the pilot may label their articles too:
the stage labels every actively covered ticker. Check the per-ticker lines in the log.

### 11. Draw the 30 labels

```powershell
$m = Invoke-RestMethod "$PUBLIC/latest.json"
Invoke-WebRequest $m.database.url -OutFile "$env:TEMP\ms-rollout\pilot.db"
@'
import random, sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = db.execute(
    """
    SELECT r.article_fingerprint, a.ticker, a.published_at, a.source, a.title,
           r.role, r.confidence, r.rationale
    FROM article_company_roles AS r
    JOIN articles AS a ON a.fingerprint = r.article_fingerprint
    WHERE r.prompt_version = 'company-role-v1'
    ORDER BY r.article_fingerprint
    """
).fetchall()
print(f"labels available: {len(rows)}")
sample = random.Random(20261007).sample(rows, 30)
for i, (fp, ticker, published, source, title, role, conf, why) in enumerate(sample, 1):
    print(f"\n{i:>2}. [{ticker}] {published[:10]}  {source}  ({fp[:12]})")
    print(f"    {title}")
    print(f"    label: {role}  confidence: {conf:.2f}")
    print(f"    why:   {why}")
'@ | uv run python - "$env:TEMP\ms-rollout\pilot.db"
```

Check: `labels available` is 200. If it is lower, the pilot did not finish; do not review a partial
set.

### 12. Apply the acceptance rule

For each of the 30, decide whether the stored role is correct: `principal` if the company is a
party to the main development the headline reports, `mentioned` if it is only context for someone
else's. Judge the role, not the rationale's wording.

- **27 or more correct:** proceed to Part C.
- **26 or fewer correct:** no bulk dispatch. The prompt changes as a new prompt version, the pilot
  is repeated, and a fresh random 30 is reviewed.

Record the count and the date in `docs/planning/WORKSTREAMS.md` either way.

## Part C — Only after the pilot passes

Bulk backfill for NVDA and PFE, in dispatches of at most 1,000:

```powershell
gh workflow run coverage.yml -f tickers=NVDA,PFE -f max_new_roles=0 -f max_new_roles_total=0 -f max_backfill_roles=1000
gh run watch
```

Repeat until the log shows `deferred_backfill=0` for each ticker. After each run: green, both
integrity checks and the checkpoint passed, tokens within plan. Stop on any `provider_unavailable`
or `circuit_breaker`.

Steady state comes after the backfill, as a reviewed commit that changes the two workflow fallback
defaults (`MAX_NEW_ROLES` to `10`, `MAX_NEW_ROLES_TOTAL` to `25`; `MAX_BACKFILL_ROLES` stays `0`).
Then MR-003 resumes.

## Not verified

These come from reading the code and the workflow, not from running them:

- that GitHub refuses to dispatch a disabled workflow (the reason step 7 enables first);
- how the Render service deploys; step 6 assumes a manual deploy from the dashboard;
- the exact wording of the `role stage:` and `role ledger:` log lines on a real run;
- that the label table reaches the published snapshot unchanged. Step 8 checks this before any
  spend.
