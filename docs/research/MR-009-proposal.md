# MR-009 — Proposal: backfilling months 13–36 (NVDA and PFE, Google News RSS)

Drafted 2026-10-07; second pass 2026-10-08, both offline. Nothing was fetched, no real database
was opened, no backfill or model call ran. Everything below comes from reading the code and from the
tests in `tests/test_backfill_offset.py`, `tests/test_backfill_boundary.py` and
`tests/test_backfill_workflow.py`.

**Settled by Alfred after the first pass:** the range ends exactly at the start of stored history,
with no re-fetched weeks; it runs as a manual workflow dispatch, one ticker per dispatch; no paid
analysis; 5.25 s pacing; a loud guard for the 5,000-article read cap. The second pass built those
(§1 "Exact boundary", §2, §4, §5). Still **for Alfred**: the role-label budget (§3) and whether to
move the read cap (§5).

## Summary

- **Second pass.** `--until-stored-start` ends the range at the earliest stored `published_at`
  (read from the database the run uses); `--until TIMESTAMP` overrides it; `--plan-only` prints the
  boundary and the buckets with no network call and no write. A run stores nothing at or after the
  boundary. A scored-article read that reaches the 5,000 cap is never used: the run refuses to
  start at the cap and stops, exits 3 and writes no rebuild if storing passes it. The workflow gets
  one ticker input, one plan-only input and one step (§2).

- `--as-of` cannot do this. The script refuses it in backfill mode, and it was built for replaying
  a past run. A small explicit option is needed, and is drafted and tested.
- One more thing was missing: the backfill script tries GDELT first. Without a switch, a run could
  pull GDELT articles into the stored Google-only sample. The draft adds `--google-only`.
- The run needs **no Stage A/B/C spend**, and can be made incapable of it.
- Deep articles become `baseline` in the analysis ledger, so scheduled runs will not analyse them.
  They become `pending` role jobs and are labelled only when a backfill role cap is raised.
- Role labels are about **4,000** (range 3,500–4,700), not 2,000: MR-007's "about 2,000" was for
  months 13–24 only. About $0.71 at the plan token figure; **tokens are unmeasured** (no pilot yet).
- Two things Alfred should know before approving: the oldest ~6–8 days of the 36-month horizon get
  no `daily_sentiment` row (§5), and one stray early article could pull the stored-start boundary
  earlier than the real start; the plan-only dispatch shows it (§1). (The first pass's
  "about 14 months stored, 46 days re-fetched" is resolved by the exact boundary.)

## 1. The change, and the exact command

### Is `--as-of` enough? No.

| question | finding | evidence |
|---|---|---|
| Would the geometry match? | Yes. `plan(now − 360 d, horizon 720 d)` equals `plan(now, horizon 1080 d, skip 360 d)` bucket for bucket. | `test_as_of_would_have_planned_the_same_buckets_but_the_cli_does_not_allow_it` |
| Can the script be told to? | **No.** `--as-of` is rejected unless `--mode fill-selection-gaps` (`scripts/backfill_historical_intelligence.py:285`), and that mode never fetches. A test pins the rejection (`tests/test_backfill_cli.py`, `test_as_of_is_rejected_outside_the_fill_selection_gaps_mode`). | code, test |
| What would lifting the guard change? | `now` is the only thing `--as-of` replaces. In `backfill()` it sets the bucket plan, the provider's `since`/`until` filters and the sentiment-rebuild start (`backfill_service.py:85–125`). It touches no ledger state and no watermark (backfill writes neither). | code |
| Is it safe? | It would work, but it is a misuse. The guard exists so a replay timestamp cannot silently drive a paid, writing run. The report would not say the run was shifted (`BackfillRunReport` has no as-of field), so a printed report could not show "only months 13–36". | code |

Verdict: not a safe way to do it. Add an explicit option.

### The draft (working code and tests on this branch, not integrated)

| file | change |
|---|---|
| `src/marketsentinel/historical_backfill.py` | `plan_backfill_buckets(now, horizon_days, offset_days=0)`. Only the end of the span moves, to `now − offset`. The horizon start and every month boundary stay those of a plain longer run. `offset_days` < 0, or ≥ `horizon_days`, raises `ValueError`. Still pure. `BackfillRunReport.offset_days` (default 0); the header names the skip only when non-zero. |
| `src/marketsentinel/backfill_service.py` | `backfill(..., offset_days=0)` passes it to the planner and the report. Needed because the service plans its own buckets. |
| `scripts/backfill_historical_intelligence.py` | `--skip-recent-months N` (backfill mode only; same 30-day month as `--months`; rejected with a clear message if ≥ `--months` or negative). `--google-only` (no GDELT contact). `--request-interval-seconds` (default 0). With `--max-new-analyses 0` the unavailable provider is wired even when a key is set. |
| `src/marketsentinel/sources/historical.py` | `GoogleNewsHistoricalProvider(request_interval_seconds=0.0, sleeper, monotonic)`; paces the RSS request and each redirect resolution. Default 0 reads no clock and never sleeps. |

Allowed-scope note: `backfill_service.py` was touched because the offset cannot reach the planner
otherwise; `sources/historical.py` because pacing lives in the provider.

The second pass adds to the same files: `until=` on the planner and `BackfillRunReport.until`;
`resolve_backfill_boundary`, `BackfillPlanReport` and `BackfillRefusal` (all pure) in
`historical_backfill.py`; `stored_history_start`, `resolve_anchored_boundary`,
`plan_anchored_backfill`, `ScoredReadCapExceeded`, the guarded `_read_scored` and a
`scored_read_cap` constructor argument (default unchanged at 5,000) in `backfill_service.py`; the
boundary options, `ReadOnlySQLiteRepository` and the exit codes in the script; and the dispatch
inputs and step in `coverage.yml` (§2). Scope note: nothing in the spec, `DECISIONS.md`, planning
documents, the SQLite schema, the ledger or coverage rules, the role stage, the `mr-v1` engine or the
GDELT code was touched.

With none of the new options set, planned buckets are unchanged (`test_with_no_boundary_the_plan_is_what_it_was_before`, `test_offset_unset_plans_exactly_what_it_planned_before`), the provider wiring is unchanged (`test_google_only_removes_gdelt_and_the_default_keeps_it`), and every test that existed before this work passes unmodified. The full suite is 1,303 passed (the first pass's 1,247 plus the new tests).

### Exact command (settled: the range ends exactly at the stored start)

Run once per ticker, from the repository root, in the environment described in §2 (the workflow
step runs exactly this, plus `--plan-only` on a plan dispatch):

```
uv run python scripts/backfill_historical_intelligence.py --ticker NVDA --months 36 \
  --until-stored-start --google-only --max-new-analyses 0 --request-interval-seconds 5.25
```

and the same with `--ticker PFE`. Look first, with the same line plus `--plan-only`: it resolves the
boundary and prints it with the planned buckets, makes no network call and writes nothing.

The first-pass `--skip-recent-months 12` is still in the script (it was tested and nothing depends
on removing it) but is **not** the command: its month arithmetic could not hit the stored start and
re-fetched about 46 days. The three boundary options are mutually exclusive:

| option | meaning |
|---|---|
| `--until-stored-start` | end = earliest `published_at` among the ticker's non-demo stored articles, read at run time from the database the run uses |
| `--until TIMESTAMP` | explicit end; ISO-8601 with a UTC offset (a naive value is refused, never assumed UTC) |
| `--skip-recent-months N` | first-pass relative offset, unchanged |
| `--plan-only` | needs one of the first two; prints and exits |

**How the end is kept exact** (`historical_backfill.py`, `backfill_service.py`):

- `plan_backfill_buckets(now, horizon_days, offset_days=0, until=None)`. With `until`, only the
  end moves: the horizon start and every month boundary are those of a plain 36-month run
  (`test_an_anchored_plan_keeps_every_earlier_boundary_of_a_plain_run`); the bucket holding the
  boundary is clipped to it; a boundary on a month start never yields a zero-width bucket. `until`
  outside `(now − horizon, now]`, or with an offset, is rejected.
- The providers bound a window inclusively (`since <= published_at <= until`, `sources/historical.py:359`),
  and Google's `before:` is whole-day. So the service additionally drops any fetched article with
  `published_at >= boundary` before storage (`_fetch_and_score_bucket`). Only the anchored mode does
  this; other modes are untouched. The test fake returns an article exactly at the end of every
  window and one a day past it: the final two are never stored, the earlier buckets' edge articles are.
  This also means the stored article at the boundary is never re-stored.
- The boundary is resolved and checked before the first constituent lookup or fetch. It is refused
  (exit 2, message on stderr, nothing fetched or written) when the ticker has **no stored non-demo
  articles** (an empty corpus means a first backfill, not a deeper one) or the boundary is **outside
  the horizon** (stored history already reaches past 36 months, or the override is in the future).
  A missing database file is refused too, without creating one.

**Plan-only** (`--plan-only`) prints: the run time and horizon; the boundary, its source and its age
in days; the earliest stored article; the **five earliest stored `published_at` values**; the
**stored non-demo count in the 30 days after the boundary** (and the total); the scored-article
count against the read cap; and every planned bucket with its start and end. It builds no service
(no constituent lookup, no news provider, no FinBERT), and reads through a connection opened
`mode=ro&immutable=1` (`ReadOnlySQLiteRepository`): it cannot write, and it does not create the
`-wal`/`-shm` side files that a plain read-only open leaves beside a WAL-mode database (the first
test version caught exactly that). The database file's bytes and the directory listing are
asserted unchanged, and `socket` connection is patched to fail the test
(`test_plan_only_prints_the_plan_and_makes_no_network_call_and_no_write`). Caveat: `immutable`
ignores an unmerged `-wal`, so run it on a database no writer has open; the workflow's downloaded
copy is one.

For a run on 2026-10-07 with a stored start of 2025-08-27 (MR-007's figure; plan-only gives the real
one) the plan is **23 buckets**, `2023-10` (partial, from 2023-10-23 12:00) to `2025-08` (partial,
ending exactly 2025-08-27 00:00). A re-run is idempotent.

### Can the stored corpus hold an article older than the first backfill's range?

Stated from the code. **The writers cannot, but the corpus is not guaranteed to start at the first
backfill's horizon, and one case would make the boundary wrong.**

- `backfill()` filters `since <= published_at`, so its first run (360-day horizon) stores nothing
  before its horizon start (`sources/historical.py:359`; GDELT applies the same bound).
- The live paths start recent: the cycle's `_ingest` floors at each source's `max_lookback`
  (`coverage_cycle.py:219`), and the interactive refresh looks back `news_lookback_days` (default 7,
  at most 30; `config.py:132`). An upsert never moves `published_at` earlier
  (`MAX(articles.published_at, excluded.published_at)`, `storage/sqlite.py:277`).
- What the code cannot rule out: rows written **before** those paths, or by a run with a larger
  horizon than 360 days, for instance the database that was uploaded to R2 to seed it ("the current
  data/marketsentinel.db", workflow comment), or a manual `--months 24` experiment. Demo rows are
  excluded from the boundary, but a stray real row is not.
- **Why it matters:** one isolated old article would set `--until-stored-start` earlier than the
  dense start. The run would then fetch only up to that stray date and leave the real gap
  (stray date to dense start) unfetched, with no error.
- **How plan-only shows it:** (1) the boundary's age, which should be about 360–400 days for the
  first backfill's horizon plus the days since it ran, not much more; (2) the five earliest
  `published_at` values should be close together, a normal start rather than one date followed by a
  long gap (`test_the_plan_names_the_oldest_stored_values_so_an_isolated_early_article_shows`);
  (3) the stored count in the 30 days after the boundary should be a normal month (MR-007: about
  72–98 relevant per bucket), not 1 or 2. If any of the three looks wrong, do not run: pass the
  true date with `--until` instead.

### Overlap with what is stored (resolved: none)

The first-pass analysis of the overlap is kept because it explains why the exact boundary was
chosen. The first backfill appears to have run about 2026-08-21 with a 360-day horizon (inferred
from the test fixtures and MR-007's stored start of 2025-08-27, §3), and live coverage has kept
growing the corpus since. "Skip 12 months" from a run on 2026-10-07 ended the range at 2025-10-12,
about **46 days** after the stored start, so those days would have been fetched twice. The
anchored run fetches none of them. For reference, why a repeat is safe anyway:

- It is safe. Rows are keyed by `sha256(title, ticker, source, published_at)`
  (`normalization.py:65`) and written with `ON CONFLICT(fingerprint) DO UPDATE`
  (`storage/sqlite.py:271`), so a repeat is an update, not a duplicate. It does overwrite
  `title`, `url`, `provider` and `fetched_at` on those rows, and stored analyses are keyed by
  fingerprint, so they stay attached. A changed `url` can change a neighbour's evidence
  fingerprint; that is reused, not re-paid (ledger rule), and regeneration is the explicit
  `refresh-evidence` mode only.
- Near-duplicates that differ in title are not removed against stored rows: `backfill_service.py`
  does not consult stored dedupe keys, only in-batch deduplication
  (`sources/historical.py`, `deduplicate_with_diagnostics`). That is the same behaviour as the
  first backfill.

## 2. Where it runs

The live corpus is the private SQLite database in R2 (`state/marketsentinel.db`). The scheduled
worker is its only writer (`.github/workflows/coverage.yml`: download at line 105, checkpoint at
291). The backfill script writes to `get_settings().database_path`, which defaults to the relative
`data/marketsentinel.db` (`config.py:38`).

| | A. Manual dispatch inside the existing workflow (recommended) | B. Local run on a downloaded copy, schedule disabled, then upload |
|---|---|---|
| Needs | A small addition to `coverage.yml` (below). Main pushed with the draft merged. | `aws` CLI and R2 keys on the laptop, `gh workflow disable`, the `ml` extra and FinBERT model locally, `MARKETSENTINEL_DATABASE_PATH` pointed at the copy. |
| Serialisation | The workflow's `concurrency: scheduled-coverage` group (lines 54–56, `cancel-in-progress: false`) queues the dispatch behind any running cycle; steps in one job are sequential. | By hand: nothing stops a queued or dispatched run from writing R2 while the copy is out. Upload overwrites R2: **lost-update risk** for anything the worker wrote meanwhile. |
| Safety rails already there | Fails closed on a missing database; integrity check before checkpoint; checkpoint with retries; the rolling `.bak`. | You must repeat them by hand. The default `database_path` is the live path: a missing env var writes to the wrong file. A `.env` may hold an LLM key. |
| Credentials | No new secret. The backfill step receives no OpenAI secret (steps get secrets only when named). | R2 write keys on a personal machine. |
| Can go wrong | Job timeout (90 min); a failed later step loses the unsaved backfill (free to redo). | Machine sleep; partial upload; schedule fired between disable and download. |
| Security boundary | Unchanged: still the one private writer; the public side gets only the sanitised snapshot afterwards. | Unchanged, but widens where private writes can happen. |

**Recommend A**, one ticker per dispatch.

### The addition to `coverage.yml` (built; this is the only change to that file)

Two dispatch inputs and one step. The step is named "Historical backfill to the stored start
(manual dispatch only)".

| piece | what it is |
|---|---|
| `backfill_ticker` | string, default `""`. **One** ticker. Empty means no backfill, which is also what every scheduled run gets |
| `backfill_plan_only` | boolean, default `false` |
| `if` | `github.event_name == 'workflow_dispatch' && github.event.inputs.backfill_ticker != ''`. A scheduled run fails both halves |
| `env` | only `BACKFILL_TICKER` and `BACKFILL_PLAN_ONLY`. **No `secrets.*`, so no OpenAI key.** The ticker reaches the shell only through the environment, never expanded into the script text |
| validation | `[[ "$BACKFILL_TICKER" =~ ^[A-Za-z][A-Za-z0-9.-]{0,9}$ ]]`, before anything runs. `NVDA,PFE`, `NVDA PFE`, `,NVDA` and `NVDA;ls` all fail with `::error::` and exit 1. The script validates the same shape again |
| command | `--ticker "$BACKFILL_TICKER" --months 36 --until-stored-start --google-only --max-new-analyses 0 --request-interval-seconds 5.25`, plus `--plan-only` on a plan dispatch |
| position | right after "Activate coverage", **before** "Sync public requests from R2", "Admit public requests", the post-admission integrity check and checkpoint, "Run bounded coverage cycle", and the integrity check and checkpoint that follow the cycle |

**Why it sits before admission, not just before the cycle.** The first-pass proposal put it after
the public-request steps. That cannot make plan-only write-free: admission spends under the OpenAI
key and its checkpoint writes R2 before the plan step would even run. Placed first, a plan dispatch
stops before any of them. The cost is nothing: the backfilled rows are on disk before the
integrity check and checkpoint that follow, and the `.bak` upload (the pre-run copy from the
download step) still holds the state before the backfill.

**Plan-only: the job stops, on purpose.** After printing the plan the step ends with `exit 1` and a
`::notice::` saying "Plan only: stopping the job here on purpose. Nothing was fetched or written
and no later step ran." Reason: GitHub gives no way to end a job early and green, and every later
step writes (admission, cycle, checkpoint, snapshot, a Render restart). Gating each of them on the
input would edit the rest of the file. So a plan-only run shows **red by design**; it is read in the
log, not in the status. A plan dispatch needs no spend caps at 0, because nothing after it runs.

**A real backfill dispatch fails closed on spend.** The steps after this one run in the same job,
so before fetching, the step requires `MAX_NEW_TOTAL`, `MAX_ARTICLE_REQUESTS`,
`MAX_NEW_ROLES_TOTAL` and `MAX_BACKFILL_ROLES` to be exactly `0` and otherwise exits 1 with the name
of the offending one. See the next section for why this guard exists.

#### Can a dispatch set the caps to 0, and what does the cycle do?

From the code: yes, and with the guard above a backfill dispatch is not allowed to run otherwise.

| input | reaches | at 0 |
|---|---|---|
| `max_new_total` | `run_coverage_cycle.py --max-new-total` → `run_all(max_new_total=0)` | `validate_arguments` rejects only negatives. `_analyze_many` starts with `remaining_total = 0` and its `while` needs `remaining_total > 0`, so the loop never runs: **no article is processed, no paid attempt**; every queued job is reported `deferred` with stop reason `budget` (`coverage_cycle.py:886`, `:949`) |
| `max_new` (per ticker) | `--max-new` → `max_new_per_ticker=0` | **alone it does not stop spend.** The per-ticker cap is checked after a turn (`queue.paid < max_new_per_ticker`, line 937), so with `max_new_total` unset or positive each ticker still gets one paid attempt. It is safe only together with `max_new_total=0` |
| `max_article_requests` | `Admit public requests` → `admit_public_requests` | `len(articles) >= 0` is true at once: every article request is deferred (not consumed, still queued in R2), `runner.process` is never called (`public_requests.py:664`) |
| `max_new_tickers` | same step | `len(activated) >= 0` is true: a coverage request for a ticker that is not yet active is deferred and left queued; one already active is consumed as a no-op (`:646`, `:650`). Activation spends nothing either way |
| `max_new_roles_total`, `max_backfill_roles` | cycle | with all three role caps 0 the role budget is not `enabled`, `_label_roles_many` returns at once (`coverage_cycle.py:985`) |

So with `max_new_total=0`, `max_article_requests=0` and the role caps 0 the cycle still **ingests**
live news (free, network), **reconciles** (gives the deep articles `baseline` ledger state, free),
checks evidence and reports; it makes no paid call. `max_new=0` and `max_new_tickers=0` may be set
too, as the runbook does, but they are not what makes it safe.

**One thing the code cannot settle: how GitHub treats the string `"0"`.** The workflow reads each
cap as `github.event.inputs.max_new_total || '40'`. If the runner treated `"0"` as falsy the
expression would fall back to the default and a dispatch could not set zero. GitHub's documentation
lists `0` among the falsy values, and does not say whether an input's *string* `"0"` is coerced;
this could not be checked offline. The guard makes the answer harmless: if `"0"` fell back to its
default, the value in the environment would be `40` and the backfill step would fail before
fetching, leaving nothing written. If that happens, the cap cannot be set to zero by dispatch and
the fix (a one-line default change) is Alfred's call; say so rather than working around it.

**Concurrency with the coverage cycle.** The script takes no ledger lease and warns not to run beside
a cycle (`scripts/backfill_historical_intelligence.py`, `concurrency_warning`). The step runs
before the cycle step in the same job, and the workflow group admits one run at a time, so no cycle
runs for that ticker during it.

**Timing.** Per ticker, with a stored start of 2025-08-27: 23 RSS requests plus up to 10 redirect
resolutions per bucket (the provider default, `sources/historical.py:248`) is at most **253
requests** (299 if every RSS request needs all three retries). At 5.25 s that is up to about 22
min of pacing, plus page latency and FinBERT scoring of about 1,700–2,400 articles (unmeasured).
Dispatch NVDA and PFE separately; each stays well inside the 90-minute job limit.

## 3. Spend

### Paid Stage A/B/C analysis: none (recommended), and a run can be made unable to spend

| run | what happens | source |
|---|---|---|
| key set, default `--max-new-analyses 60` | each bucket selects up to `--bucket-candidate-cap` (5) candidates and calls the provider until 60 new attempts | `backfill_service.py` `_select_and_analyze_bucket`, `_AnalysisBudget` |
| key unset | provider is `UnavailableArticleAnalysisProvider`; the first call returns `unavailable`, which writes nothing; two in a row trip the breaker. No spend, but up to 2 attempts and a "circuit breaker tripped" report | `event_analysis.py:230`, `backfill_service.py` `_AnalysisBudget.record` |
| **`--max-new-analyses 0`** | the budget is stopped before the first bucket, so `analyze_article` is never called; articles are still fetched, stored, scored, and daily sentiment rebuilt | `backfill_service.py:369` and `test_an_offset_run_with_a_zero_analysis_budget_stores_and_scores_but_never_analyses` |

The guarantee is `--max-new-analyses 0`. The draft also wires the unavailable provider in that case
even if a key is present, so a stray `.env` key cannot be used
(`test_a_zero_analysis_budget_wires_the_unavailable_provider_even_with_a_key`). `mr-v1` needs scored
articles and role labels, not Stage A/B/C, so nothing is lost.

**Does the worker then analyse 24 months of old articles?** No. On the next cycle `_reconcile`
(`coverage_cycle.py:736`) gives each new article a ledger state; anything published before
`ledger_started_at − live_window_days` is `baseline` ('pre_ledger_history', line 751), and only
`pending` or `retry_wait` jobs are claimable. Shown by
`test_deep_history_becomes_baseline_and_waits_for_an_explicit_role_budget`: all new rows
`baseline`, zero Stage A/B/C calls with a 1,000 cap. The baseline write is free.

### Role labels

Each non-demo article gets exactly one `pending` role job (`reconcile_role_jobs`,
`company_role_ledger.py:338`), however old. The role stage splits work into *new* (inside the live
window) and *backfill* (everything older, `is_new_article`, line 291). Deep articles are all
backfill, so scheduled runs with `MAX_BACKFILL_ROLES=0` and steady-state new caps never label them
(same test). They are labelled only under `max_backfill_roles` (`coverage_cycle.py:1073`), in
the order: articles in sessions with at least 3 distinct sources first, then newest first, ties by
fingerprint (`role_processing_order`, line 325). Priority is computed across all stored articles, so
the 12-month corpus's priority articles are drained before the deeper ones.

**Count.** `L = Σ over tickers of N_t`, where N_t is the number of non-demo articles newly stored
(fingerprint-distinct), plus one extra paid call per failed attempt that is retried.

| basis | N per ticker | L (NVDA + PFE) |
|---|---|---|
| MR-007's "about 1,000 per ticker per 12 months", × 24 months | 2,000 | **4,000** |
| per-bucket yield 72–98 relevant, 24 months of width | 1,730–2,350 | 3,460–4,700 |
| ceiling: Google's 100-entry page × 24 months | 2,400 | 4,800 |

Dispatches of at most 1,000 (approved rule): 4 to 5 for the new articles. The approved role budget
(2,852 stored on 2026-10-07) does not cover them; this is a **new budget decision**.

**Cost** = `L × (T_in × 0.15 + T_out × 0.60) / 1,000,000` at `gpt-4o-mini` list price ($0.15 in,
$0.60 out per 1M tokens, checked 2026-10-07).

| token basis per label | $ per label | L = 4,000 | L = 3,460–4,700 |
|---|---|---|---|
| MR-006 plan, 900 in / 70 out | 0.000177 | **$0.71** | $0.61–$0.83 |
| MR-006 ceiling, 1,100 in / 100 out | 0.000225 | $0.90 | $0.78–$1.06 |
| MR-007 assumption, 350 in / 30 out | 0.0000705 | $0.28 | $0.24–$0.33 |

**Unmeasured.** The pilot has not run, so there are no measured tokens per label. MR-006's figures
are plan estimates from character counts. MR-007's 350 / 30 is a back-calculation from MR-003 that
conflicts with MR-006's 900 / 70; use MR-006's until the pilot reports. Recompute after the pilot's
`role ledger:` line. Including the 2,852 already approved, the total is about 6,850 labels, about
$1.21 at plan.

## 4. Runbook (option A, per ticker)

Do not start before: schema-6 rollout verified (checklist step 8); pilot passed (27 of 30) and
recorded; this change approved and merged to `main`, pushed, CI green; the workflow addition
merged; the label budget approved.

1. **Confirm idle.** `gh run list --workflow coverage.yml --limit 3`: none `in_progress` or
   `queued`. The cron fires at 00:00, 06:00, 12:00, 18:00 UTC; start well clear of them.
2. **Back up.** `aws s3 cp "$PRIVATE/state/marketsentinel.db" "$PRIVATE/backups/marketsentinel-pre-mr009-$STAMP.db" --endpoint-url $ENDPOINT`;
   check sizes equal. Keep `$STAMP`.
3. **Record the before-state** from a read-only download of the backup (`sqlite3 ... ?mode=ro`):
   per ticker `COUNT(*)`, `MIN(published_at)`, `MAX(published_at)` of `articles`; counts of
   `article_company_roles`, `article_intelligence_analyses`, `daily_sentiment`. Write the numbers down.
4. **Plan-only dispatch first.** It reads the downloaded database, prints, and stops the job:
   `gh workflow run coverage.yml -f backfill_ticker=NVDA -f backfill_plan_only=true`, then
   `gh run watch`. **The run ends red on purpose** (§2); the log is the result. Nothing was fetched
   or written and no later step ran. Read the plan block and decide before going on:
   - the **boundary** is the stored start you expect (MR-007: about 2025-08-27) and about 360–400
     days old, not much older;
   - the **five earliest `published_at` values** sit close together;
   - the **30-day count after the boundary** looks like a normal month (tens, not 1 or 2);
   - the **scored-article count** is comfortably below the 5,000 cap (§5);
   - the **buckets** run from about 2023-10 to the boundary's month, and the last one ends exactly
     at the boundary.
   If the boundary looks like an isolated early straggler (§1), do not run: take the true date and
   pass it by changing the step's `--until-stored-start` for `--until`, which is a workflow edit
   for Alfred. Write down the boundary `B` and the scored count.
5. **Dispatch the backfill, spending nothing else.** Every paid cap must be `0` or the step
   refuses before it fetches:
   `gh workflow run coverage.yml -f backfill_ticker=NVDA -f max_new=0 -f max_new_total=0 -f max_new_tickers=0 -f max_article_requests=0 -f max_new_roles=0 -f max_new_roles_total=0 -f max_backfill_roles=0`
   then `gh run watch`.
6. **Check the log.** The line `Boundary: nothing at or after <B> will be stored.` shows the same `B`
   as the plan. The report header reads `(horizon: 1080 days, ending exactly at <B> (nothing at or
   after it))`; about 23 bucket lines from `2023-10` to the month of `B`; every bucket `partial`.
   **`partial` is the normal status** (the Google provider always reports `degraded`); only
   `failed` is a problem, so list any. `Total new analyses attempted: 0`, circuit breaker `False`.
   No OpenAI call in the log. Integrity checks and the checkpoint green.
   **If the log instead shows `BACKFILL STOPPED: SCORED-READ CAP EXCEEDED` (exit 3)**, the step
   failed before any checkpoint: R2 is unchanged and nothing needs undoing (§5). Stop and tell
   Alfred.
7. **Verify nothing at or after `B` was fetched.** On a read-only download of the new `state/`
   database (`sqlite3 ... ?mode=ro`), with `<start>` the dispatch's start time (UTC, ISO-8601):
   - `SELECT COUNT(*) FROM articles WHERE ticker='NVDA' AND provider='Google News RSS historical-range fallback' AND fetched_at >= '<start>' AND published_at >= '<B>'` must be **0**.
     (The live cycle that runs later in the same job ingests recent news too, so the provider
     filter matters: only the historical provider's rows belong to this check.)
   - `SELECT MAX(published_at), MIN(published_at), COUNT(*) FROM articles WHERE ticker='NVDA' AND provider='Google News RSS historical-range fallback' AND fetched_at >= '<start>'`:
     `MAX` is below `B`; `MIN` is about the run date − 1080 days; the count is the total of the
     bucket lines' `articles=`.
   - Rows of the earlier corpus are untouched: `SELECT COUNT(*), MIN(published_at) FROM articles WHERE ticker='NVDA' AND published_at >= '<B>' AND published_at <= '<before-snapshot time>'`
     equals the step-3 numbers for the same range (the first article's `fetched_at`, `url` and
     `provider` are not rewritten, because it is never fetched again).
8. **Next cycle.** In the following scheduled log, the reconcile line shows N new `baseline` jobs, Stage A/B/C
   paid attempts unchanged, `role stage: claimable_backfill` up by N.
9. **Repeat for PFE** (plan-only first). Then role-label dispatches (`max_backfill_roles=1000`) only
   after the budget in §3 is approved.

**Roll back.** A dispatch that fails, or is plan-only, never reaches a checkpoint: R2 is unchanged.
After a green backfill: `gh workflow disable coverage.yml`, then
`aws s3 cp "$PRIVATE/backups/marketsentinel-pre-mr009-$STAMP.db" "$PRIVATE/state/marketsentinel.db" --endpoint-url $ENDPOINT`,
re-enable. The backfill is free and idempotent, so the usual remedy is to rerun it. Restoring after
role labels were paid for loses those labels, so roll back before the first label dispatch.
The rolling `.bak` is overwritten by the next run; rely on the named backup.

## 5. What the deeper history does to `mr-v1`

- Per the company-role decision (placement (a)), the provisional `0.42` / `0.20` do not carry over:
  thresholds are **re-selected on the larger labelled pool**, from sentiment only, and the
  funnel-count and history-sufficiency check is written down before any return is read.
- **MR-003 resumes only after the backfill and all its labels are complete.** An unlabelled
  article is excluded, so a half-labelled deep history is a different, smaller pool.
- Depth, as MR-007 extrapolated linearly and before the role filter shrinks it by an unmeasured
  share: at 36 months about NVDA 21 negative / 45 positive and PFE 36 / 45 events, so NVDA negative
  is the regime that may still sit near the n ≥ 20 line.
- One regime, not two: the new months are Google, 30-day buckets, date-only like the stored ones
  (MR-007 §4). They add thin-regime sessions; the dense post-2026-08-17 live era becomes a smaller
  share of the pool. The chronological split-half still compares regimes at its seam; this does not
  remove that.
- **Known edge:** the daily-sentiment aggregation decays by age with a 24 h half-life and
  underflows to zero weight beyond about 1,072 days (probed: a 1,070-day-old article yields a row,
  1,075 does not). The oldest ~6–8 days of the 36-month horizon get **no `daily_sentiment` row**
  (display series only; `mr-v1` reads scored articles, not that table). The rebuild also spans the
  last 12 months, deleting and re-deriving those rows from the same stored articles. It should
  reproduce them (weights within a day differ by a fixed ratio whatever the compute time), but that
  was not tested, and it is how the first backfill already behaved.
- **Read cap, now guarded (second pass).** The rebuild and every per-bucket read were capped at
  5,000 scored articles, newest first (`backfill_service.py`, `_SENTIMENT_AGGREGATION_LIMIT`), so a
  larger corpus silently lost its **oldest** rows, which is exactly the range this run adds. The cap
  is **not raised**. What changed is that a result at the cap is never used:
  - Every scored read in the backfill (per-bucket, the rebuild, and the selection-gap fill) now asks
    for `cap + 1` rows and treats more than `cap` as truncated.
  - **Before any fetch:** if the ticker's scored articles since the horizon start already number
    `cap` or more, the run refuses (exit 2, `Nothing was fetched or written`). Exactly at the cap
    counts, because a read of exactly `cap` rows cannot be told from a truncated one.
  - **After storing:** if a read exceeds the cap, a `ScoredReadCapExceeded` is raised, the CLI
    prints a `BACKFILL STOPPED: SCORED-READ CAP EXCEEDED` block on stderr and exits **3**. The
    daily-sentiment rebuild is never computed from a truncated read: the read happens before the
    delete and the upsert, so `daily_sentiment` is exactly what it was.
  - **Database state after a stop.** Mid-fetch: the buckets up to and including the one that
    overflowed are stored and scored; later buckets were not fetched; no analysis ran for that
    bucket; `daily_sentiment` is untouched. At the rebuild: every bucket is stored and scored (any
    analysis within the budget ran); `daily_sentiment` is untouched. In the **workflow** the step
    fails, so no checkpoint runs and the database in R2 is unchanged: nothing to undo. On a local
    database the stored rows stay and are harmless, but **a re-run refuses to start** (the corpus
    is now at or past the cap).
  - **What the operator does next:** nothing is wrong with the data. Stop and tell Alfred; moving the
    cap is a product-owner decision (below). Do not re-run, and do not edit the constant to get past it.
  - Tests: at the cap (refused before any fetch), one below (completes), one above (stops, daily
    sentiment untouched), an overflow only at the rebuild, and the CLI exit codes.
  - Other capped reads: `_STALE_BACKLOG_QUERY_LIMIT` (5,000) belongs to `reanalyze-stale` and
    `refresh-evidence`, which a boundary backfill does not run. It is unguarded and unchanged. The
    Google page itself returns at most 100 entries per window, which is the per-bucket fetch ceiling
    MR-007 already works with; the guard does not cover it.
- **Is NVDA at 36 months expected to hit 5,000?** No, by the numbers on hand, with a check that
  settles it. 2,852 articles were stored across both tickers on 2026-10-07 (§3), and the new range
  adds about 23 buckets × 72–98 = 1,650–2,250 per ticker (at most 23 × 100 = 2,300 at Google's page
  ceiling). Reaching 5,000 would need NVDA alone to hold more than about 2,700 of those 2,852 today.
  At an even split (about 1,430 each) NVDA ends near 3,100–3,700, with roughly 1,300–1,900 spare. The
  split between tickers is unknown from here, and the live cycle keeps adding, so **the plan-only
  dispatch settles it: it prints the scored count. At or below about 2,700 the run cannot reach the
  cap even at the page ceiling.** Above that, expect the guard.
- **Option for Alfred: move the cap.** Not done, and not needed for the numbers above. If wanted:
  1. *Raise the constant* (`_SENTIMENT_AGGREGATION_LIMIT`, one line), for example to 20,000. No
     schema, cache-version or ledger effect; the aggregation is deterministic and `now` stays
     injected. The cost is reading more rows per bucket: each bucket reads everything newer than its
     start, so about 23 reads of up to a few thousand rows; seconds, not minutes.
  2. *Pass `limit=None`* in those three reads. The repository's own docstring prefers it when the
     window is already the bound, and here it is (the horizon). The guard then cannot fire, and its
     code can stay as a no-op or go.
  3. *Read each bucket's own window only* (an upper `published_at` bound on the read). Largest
     change, touches `storage/sqlite.py`; it would shrink the per-bucket reads but the rebuild
     still needs the full horizon, so it does not remove the rebuild's limit.
  Recommendation: leave it at 5,000 for this run (the guard makes a surprise safe and loud), and
  take option 2 if plan-only ever shows the count above 2,700.

## Unvalidated

- Everything about Google at this depth rests on **MR-007's 13 probe requests**, 30-day windows
  centred 13 to 36 months back, paced at 5.25 s.
- Behaviour of about 23 sequential windows, of about 230 redirect resolutions per ticker (a full
  publisher page is fetched on each), and of any pacing: never tested. 429 handling for Google is
  only the 3-attempt retry on `httpx.HTTPError`.
- FinBERT runtime on the runner for about 2,000 articles per ticker; the 90-minute limit.
- Label tokens, label quality, and the share removed by the role filter.
- The `aws`/`gh` commands and the new workflow inputs: not run here. The workflow is checked only
  structurally (`tests/test_backfill_workflow.py`), plus the ticker pattern applied in Python; the
  bash itself (`[[ =~ ]]`, the indirect `${!cap}` expansion, the plan-only `exit 1`) has not been
  executed, and neither has GitHub's handling of the string `"0"` (§2).
- The stored start: 2025-08-27 is MR-007's figure and the first backfill's date is inferred. The
  real boundary, and whether a stray early article distorts it (§1), are known only from the plan.
- `immutable=1` plan-only reads on a Windows path and on the Linux runner: tested here on Windows
  only.
