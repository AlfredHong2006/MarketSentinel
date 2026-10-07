# MR-006 — Primary-company pre-analysis: proposal for approval

Status: **proposal; only the §2 schema change is approved (and applied); the rest is not settled.** Four items need Alfred's decision:
budget (§1), schema change (§2), filter rule (§3), spec amendment (§4). §5 is the runbook for the
step that happens only after those approvals.

Nothing was spent in producing this. No real LLM or API call, no key, no `.env`, no network, no
database access (counts come from the MR-003 report, not a query), no Git write. Every provider in
every test is a double, so **no test says anything about how a real model labels a real article.**

## 0. What exists and what does not

Built and tested offline on branch `ms/mr-006-primary-company` (1,147 tests pass; the 102 new ones
cover the stage, the ledger integration, the engine filter, the CLI and the workflow):

| piece | where |
|---|---|
| role vocabulary `principal` / `mentioned`, extraction schema, stored-label and response contracts | `domain.py` |
| the stage: prompt `company-role-v1`, schema `company-role-schema-v1`, providers, semantic validation | `company_role.py` |
| ledger runner, deterministic ordering, reconcile, `RoleBudget` (all zero by default) | `company_role_ledger.py` |
| coverage-cycle pass, CLI flags, workflow inputs (all default `0`) | `coverage_cycle.py`, `scripts/run_coverage_cycle.py`, `.github/workflows/coverage.yml` |
| engine filter, both placements, three unlabelled policies, result report | `market_reaction/role_filter.py`, `engine.py`, `models.py` |

Not changed: `STAGE_A/B/C_PROMPT_VERSION`, `ARTICLE_ANALYSIS_SCHEMA_VERSION`,
`analysis_compatibility.py`, `subject_principal.py`, the materiality layer, the evidence window,
frozen fixtures, the public API. `MR_V1_FROZEN_THRESHOLDS` is still unset. `evaluate_materiality`
output is byte-identical to the pre-change run.

**The SQLite label table is now applied** (§2 approved by Alfred): the `article_company_roles` DDL and
index, `SCHEMA_USER_VERSION = 6`, the three `SQLiteRepository` store methods, and the v5 → v6
migration test (`tests/test_schema_v6_migration.py`, on throwaway databases). The stage, ledger and
cycle tests now run against the real table; the in-memory store is gone. `scripts/run_coverage_cycle.py`
still refuses a positive role cap when a repository does not implement `CompanyRoleStore`, as a guard.
The scheduled workflow is unaffected until caps are raised: every default is still `0`. The rollout
order in §2 matters before this merges, because the version bump is visible to the public service.

## 1. Budget

Article counts are taken from the MR-003 report, not from a database query.

| corpus | articles |
|---|---|
| stored, non-demo, sentiment-scored (NVDA 1,703; PFE 1,149; AAPL 344; MSFT 146) | **3,342** |
| in a session with ≥ 3 distinct sources (the articles that can change an `mr-v1` result) | **2,835** (NVDA 1,423; PFE 957; AAPL 327; MSFT 128) |

Only **actively covered** tickers are labelled (the cycle runs `--all-active`), and today only NVDA
and PFE are covered: **2,852 stored / 2,380 priority**. Labelling AAPL or MSFT needs them activated
first (activation spends nothing under the existing rule).

### Tokens per call, from the actual prompt

No tokenizer is available offline and no call was made, so these use ≈ 4 characters per token. They
are a plan, to be replaced by the measured ledger totals after the pilot (§5).

| component | size | tokens |
|---|---|---|
| instructions (`_COMPANY_ROLE_INSTRUCTIONS`, 2,560 chars, 410 words) | measured | ≈ 640 |
| fenced payload skeleton incl. company, publisher, timestamp (265 chars) | measured | ≈ 66 |
| headline | assumed ≈ 90 chars | ≈ 23 |
| RSS snippet | assumed 0–1,000 chars (stored maximum 4,000) | 0–250 |
| structured-output schema added by the SDK | assumed | ≈ 100 |
| **input per call** | | **≈ 830 (no snippet) – 1,080; plan 900, ceiling 1,100** |
| **output per call** (`subject_symbol`, role, confidence, rationale ≤ 300 chars) | | **≈ 70 typical, ≤ 100** |

### Sizes, as token counts

Plan = 900 in / 70 out per call; ceiling = 1,100 in / 100 out.

| job | calls | input tokens (plan / ceiling) | output tokens (plan / ceiling) |
|---|---|---|---|
| backfill, NVDA + PFE, priority articles only | 2,380 | 2.14 M / 2.62 M | 0.17 M / 0.24 M |
| backfill, NVDA + PFE, everything stored | 2,852 | 2.57 M / 3.14 M | 0.20 M / 0.29 M |
| backfill, all four stored tickers, priority only | 2,835 | 2.55 M / 3.12 M | 0.20 M / 0.28 M |
| backfill, all four stored tickers, everything | 3,342 | 3.01 M / 3.68 M | 0.23 M / 0.33 M |
| steady state, per 6-hour run, typical (≈ 4 new articles, NVDA + PFE) | ≈ 4 | ≈ 3.6 k | ≈ 0.3 k |
| steady state, per run, at the recommended caps (worst case) | 25 | 27.5 k | 2.5 k |
| steady state, per month, NVDA + PFE (≈ 450 articles) | ≈ 450 | ≈ 0.41 M | ≈ 0.03 M |

The steady-state volume comes from MR-003 §F4: live ingestion of ≈ 290–390 NVDA and ≈ 90–106 PFE
articles per month, over about 120 runs a month. Earnings days burst well above the average, which is
why the per-run cap is several times the typical volume.

**Cost formula** (price per token left as an input; none was looked up):

```text
cost = input_tokens × P_in + output_tokens × P_out
backfill (NVDA + PFE, everything) = 2.57e6 × P_in + 0.20e6 × P_out   (plan)
                                    3.14e6 × P_in + 0.29e6 × P_out   (ceiling)
```

### Recommended caps (proposal, for Alfred to set)

| cap | value | reason |
|---|---|---|
| `max_backfill_roles` — pilot dispatch | **200** | enough to read actual tokens and judge label quality before the bulk |
| `max_backfill_roles` — each bulk dispatch | **≤ 1,000** | the job has a 90-minute timeout and the checkpoint runs only after the cycle step; an interrupted long run would lose paid labels. Call latency is unmeasured, so 1,000 (≈ 33 min at 2 s per call) is deliberately conservative. Three to four dispatches complete NVDA + PFE; each article is paid once per contract, so repeating a dispatch never double-pays. |
| `max_new_roles` (per ticker, per run) | **10** | ≈ 2.5× the typical per-ticker volume |
| `max_new_roles_total` (per run) | **25** | bounds a burst or a newly activated company |
| delivered defaults | **0 / 0 / 0** | unchanged: the scheduled workflow spends nothing on this stage until they are raised on purpose |

The backfill ceiling is not a cumulative cap: `max_backfill_roles` is a per-dispatch ceiling, and the
only cumulative spend control is that an article is never paid for twice. Cumulative token totals are
printed on every run (`role ledger: ... input_tokens=... output_tokens=... (cumulative)`).

## 2. Schema change

**One new table, `article_company_roles`. No existing table is altered.**

```sql
CREATE TABLE IF NOT EXISTS article_company_roles (
    article_fingerprint TEXT NOT NULL REFERENCES articles(fingerprint) ON DELETE CASCADE,
    model_version  TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    subject_symbol TEXT NOT NULL,
    subject_name   TEXT NOT NULL,
    role           TEXT NOT NULL,
    confidence     REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    rationale      TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (article_fingerprint, model_version, prompt_version, schema_version)
);
CREATE INDEX IF NOT EXISTS idx_article_company_roles_subject
    ON article_company_roles (subject_symbol, model_version, prompt_version, schema_version);
```

- **Immutable and versioned.** The key is the article plus the stage's own three version fields; rows
  are inserted `ON CONFLICT DO NOTHING` and never updated. A prompt, schema, or model change writes new
  rows beside the old ones. This mirrors `article_intelligence_analyses`.
- **Unlabelled ≠ mentioned.** No row means "not labelled". `role` has no `CHECK`, so adding a
  vocabulary member later is a version bump, not a table migration; the reader rejects any value it
  does not know.
- **The ledger needs no schema change.** Role jobs are rows in the existing `article_analysis_jobs`
  under a distinct contract key (`role:m=…;p=…;s=…`).
- **`PRAGMA user_version` 5 → 6** (`SCHEMA_USER_VERSION = 6`).

### What the migration does to an existing version-5 database

`SQLiteRepository.initialize()` runs the schema script, in which every statement is
`CREATE … IF NOT EXISTS`, and ends with `PRAGMA user_version = 6`. On a v5 database that:

1. creates the new table and its index, empty;
2. sets `user_version` to 6;
3. does nothing else: no `ALTER`, no row read or rewritten, no existing index touched.

It is idempotent (a second `initialize()` is a no-op), takes milliseconds, and runs in place at the
start of the first run of the new build. It is reversible: an older build opening the v6 file ignores
the extra table and resets `user_version` to 5, and the table can simply be dropped.

Planned migration test, on a throwaway database: build a frozen copy of the v5 schema with rows in
every v5 table; run `initialize()` twice; assert every v5 row is byte-for-byte unchanged, the new
table exists and is empty, `user_version == 6`, and the Stage A/B/C ledger summary is unchanged.

### Deployment interaction and safe order

Facts from the code:

- The private worker runs whatever is on the repository's default branch, downloads the R2 private
  database, migrates it in place, checkpoints it back, and then publishes a manifest stamped with
  **its own** `SCHEMA_USER_VERSION`.
- The public service, at startup, applies the published snapshot only if `manifest.schema_user_version`
  equals **its own build's** constant; otherwise it keeps the snapshot baked into the image. It never
  raises and uvicorn always starts.

So a version bump is never an outage, but a **mismatch silently degrades the public site to the
image's baked snapshot**, which can be much older than the last published one. There is no order
without a window, so the goal is to make it one worker run long and visible.

**Recommended order:**

1. Refresh the baked fallback first (`scripts/build_deployment_snapshot.py`, the existing manual
   path) so the degraded state is recent data rather than stale data.
2. Disable the scheduled workflow, then merge. Merging puts the v6 code on the default branch, so the
   very next scheduled run would otherwise be a v6 worker.
3. Deploy the public build (v6). It serves the baked snapshot, because the last published manifest is
   still version 5.
4. Dispatch **one** worker run with every role cap at `0`. It migrates the private database, passes
   the integrity gate, checkpoints v6 to R2, publishes a manifest with `schema_user_version = 6`, and
   restarts the public service, which now accepts it. End-to-end check of the bump with no spend.
5. Re-enable the schedule. Only then start the role backfill (§5).

Rollback: the rolling `.bak` generation holds the pre-run v5 database, and the zero-cap default means
the stage is inert even on the new code.

### Should the label table be in the public snapshot?

**Yes; no change to the snapshot builder.** `publish_public_snapshot.py` takes a full `VACUUM INTO`
copy of the private database, so the table is included automatically (as the job ledger already is).
Reasons to keep it: a published `mr-v1` result that used the filter cannot be reproduced without the
labels, and DECISIONS 2026-10-06 already calls an irreproducible public result a credibility problem
for this product; the content is a role, a confidence, and a ≤ 300-character description of public
headlines, passed through the same secret/personal-data scan as the headlines already in the
snapshot; and it is small (≈ 3.3 k rows, order of 1–2 MB). No public code path reads it until a later
workstream, and the public side gains no credential and no spend path.

## 3. Filter rule

The rule implemented is **`principal_only`**: an article counts when its stored label is `principal`.
Both placements and all three unlabelled policies are implemented and tested; none is chosen.

### Placements

| | (a) before signals | (b) at event qualification |
|---|---|---|
| session signal `S_t` | mean over **principal** articles only | unchanged (all articles) |
| distinct-source count | principal sources | all sources for the signal; the event additionally needs ≥ 3 principal sources |
| selection population `E` | role-filtered: only sessions that still have ≥ 3 principal sources | **unchanged** |
| thresholds | must be re-selected on the filtered pool (sentiment only) | the pre-registered 0.42 / 0.20 stay valid |
| history sufficiency | recomputed on principal-only sessions | unchanged |
| a blocked tail session | does not exist as a session | exists, never qualifies, holds no exclusivity window |

Demonstrated in the tests: an article that is only *mentioned* and strongly negative drags a session
from 0.70 to 0.30. Under (a) the session signal is 0.70 and it is an event; under (b) it stays 0.30 and
is not. That is the difference in one case: (a) cleans the quantity the product's claim rests on,
(b) only gates which sessions may become events.

### Unlabelled articles

| policy | meaning | effect |
|---|---|---|
| `exclude` | not counted, as if not principal | conservative: an event never rests on unverified articles |
| `include` | counted as principal | reintroduces the contamination for exactly the articles nobody checked |
| `session_ineligible` | a session holding any unlabelled article cannot be an event | strictest; under (a) it also removes the session from `E`, so a handful of failed labels can move the thresholds |

None treats an unlabelled article as `mentioned`, and every result reports
`articles_considered / principal / mentioned / unlabelled / excluded` and `sessions_removed`.

### Recommendation: placement (a) with `exclude`, conditional on one outcome-blind check

Reasons:

1. **It fixes the quantity, not just the gate.** The MR-003 finding was that session *sentiment* was
   often about another party. Under (b) a session still carries the contaminated mean as long as three
   principal articles sit beside the contaminating ones; under (a) the mean is about the company.
2. **It keeps the claim honest at the freeze.** The product statement is "when the news about *this
   company* was clearly positive or negative"; only (a) makes every number behind it about this
   company.
3. **`exclude` never counts an unchecked article, and a failed label costs one article rather than
   a whole session.** Under `session_ineligible` a few permanently failed labels remove sessions from
   `E` and move the thresholds; under `exclude` they remove single articles, so the thresholds move
   less. After a complete backfill the unlabelled share should be near zero, so the policy mostly
   matters during the transition and for permanent failures, which the result reports.

Costs, stated plainly:

- **Outcome-blind threshold selection changes under (a).** `E` is rebuilt from principal-only
  sessions, so it shrinks (MR-003's face-validity sample found 2 of 12 tail sessions not
  principal-driven and 4 mixed, and a mixed session can fall below 3 principal sources; by how much
  `E` shrinks is unmeasured). The quantiles are noisier, and the provisional 0.42 / 0.20 must be
  re-selected on the labelled corpus, sentiment only, before any return is read. Under (b) the
  pre-registered pair is untouched.
- **Fewer events.** The funnel was already capped at 15 resolved events per regime on NVDA/PFE;
  principal-only will lower it. This does not make the verdict tier harder to reach in a way (b)
  avoids: (b) also removes events, just fewer.
- **History sufficiency can fail** for a company with many mention-only days.

So the recommendation holds only if this check passes, and it is free of outcomes: after the backfill,
compute under each placement/policy the thresholds, the event funnel (tail → ≥ 3 principal sources →
exclusivity → resolved, counts only), and history sufficiency for NVDA and PFE. If (a) keeps both
companies sufficient, choose (a) + `exclude`; if (a) collapses sufficiency, choose (b) + `exclude`.
Either way write the result down before any return is loaded, exactly as MR-003 did.

Two related choices, recommended off for v1: **`confidence` is stored for audit but not used by the
rule** (a floor would be another numeric knob to tune), and there is no third role (a counterparty is
already a party, and all four MR-003 failure shapes are `mentioned`).

## 4. Spec amendment

**How it changes what the spec says.** The spec currently says `mr-v1` "should use the broader
sentiment-scored corpus" and "Do not require full paid LLM analysis" (§2). The amendment keeps the
*corpus* (every sentiment-scored article is still the population and is labelled) but makes a stored,
paid LLM role label a **required input of article eligibility**. It still does not require the
Stage A/B/C analysis, but `mr-v1` can no longer be computed for a company without paid labelling: a
one-off backfill per company plus every new article. That is a real departure from the current
text, so it needs a `docs/DECISIONS.md` entry; because nothing is frozen it can stay `mr-v1`
(DECISIONS 2026-10-06, "ratified as `mr-v1`").

Proposed text, assuming placement (a) and `exclude` (§3). If Alfred chooses (b) the changes to §6 and
§11 below are dropped and §2.1 describes the event-qualification rule instead.

```diff
--- a/docs/product/HISTORICAL_MARKET_REACTION_V1.md
+++ b/docs/product/HISTORICAL_MARKET_REACTION_V1.md
@@ -37,13 +37,36 @@
 Range: `[-1, +1]`.

 Eligible articles must:
 - belong to the company;
 - not be demo data;
 - have valid sentiment probabilities;
-- have a usable publication date/time.
+- have a usable publication date/time;
+- carry a stored company-role label of `principal` (section 2.1).

-Do not require full paid LLM analysis. `mr-v1` should use the broader sentiment-scored corpus.
+Do not require full paid event analysis (Stage A/B/C). `mr-v1` uses the broader sentiment-scored
+corpus as its population: every sentiment-scored article is labelled, and the label, not the event
+analysis, decides eligibility.
+
+### 2.1 Company role
+
+Each article carries one stored, versioned, immutable label for the covered company's role in the
+development it reports:
+
+- `principal`: the company is a party to the underlying event (buyer, seller, bidder, target,
+  plaintiff or defendant, contractual counterparty, regulated or investigated entity, or owner of the
+  affected asset, right, or liability);
+- `mentioned`: the company is only context for someone else's development.
+
+The label is an extraction recorded by a separate paid LLM stage with its own prompt and schema
+versions. It is never a guess: an article that could not be labelled is **unlabelled**, which is a
+different state from `mentioned`.
+
+Eligibility is a deterministic rule on the stored label, never a prompt: an article counts only when
+its label is `principal`. An unlabelled article does not count. The result must report how many
+articles were excluded by the rule and how many were unlabelled, and which label contract (model,
+prompt version, schema version) produced the labels used.

@@ -146,7 +169,8 @@
 Selection population:

 ```text
-E = pooled session signals with distinct_sources >= 3
+E = pooled session signals with distinct_sources >= 3,
+    built from principal-labelled articles only (section 2.1)
 ```

 Sessions that could never qualify as events do not shape the tails.

@@ -358,6 +382,8 @@
 Track at minimum:
 - unusable/missing publication-time share;
+- share of articles without a role label;
+- share of articles excluded as `mentioned`;
 - stock-price gaps;
 - benchmark-price gaps;
 - unresolved exchange;
 - unresolved event windows.

@@ -492,6 +518,7 @@
 They may **not** silently change:
 - signal formula;
+- the company-role eligibility rule, its vocabulary, or the unlabelled-article policy;
 - timing semantics, including the day-0 cohort rule;
```

Also needed alongside it (not text in the spec): the §11 history-sufficiency quantities
(`N_signal`, `N_eligible`, span, density) are then computed on principal-only signals, which follows
from the §2.1 definition and needs no further wording; and the threshold pre-registration is repeated
on the labelled corpus before freeze.

## 5. Post-approval runbook

Run only after §1–§4 are approved, the spec amendment and a DECISIONS entry are merged, and the
storage change in §2 is applied. Alfred performs every Git write; the steps below are the order, not
a script to run unattended. Each step lists what to check before and after.

1. **Pre-flight, offline.** `uv sync --locked --all-extras --dev`; `uv run pytest`;
   `uv run ruff check .`; `uv run ruff format --check .`;
   `uv run python scripts/evaluate_materiality.py evaluate --no-drift-check`.
   *Check:* all green; the materiality score equals the one on `main`.
2. **Confirm the budget inputs.** Read-only: the stored non-demo article count per active ticker from
   a *downloaded copy* of the private database (never the live file), and which tickers are in
   `company_coverage`. *Check:* NVDA + PFE ≈ 2,852 (priority ≈ 2,380); activate AAPL/MSFT first only if
   they are to be labelled.
3. **Back up.** Copy the R2 object `state/marketsentinel.db` to a dated key. *Check:* the copy's size
   equals the original's.
4. **Roll out the schema with no spend** — the order in §2: refresh the baked fallback; disable the
   schedule; merge; confirm the public build is deployed; dispatch one run with
   `max_new_roles=0 max_new_roles_total=0 max_backfill_roles=0`.
   *After:* the run is green including the integrity gate and checkpoint; the published `latest.json`
   has `schema_user_version: 6`; the public service logs `public snapshot: applied`; the private
   database has `PRAGMA user_version = 6`, an empty `article_company_roles`, and every other table's
   row count unchanged; `--mode status` shows the Stage A/B/C ledger unchanged. Re-enable the schedule.
5. **Pilot.** Dispatch with `max_backfill_roles=200`, new-article caps 0.
   *After:* `role stage:` shows `paid_attempts=200` and `stopped=budget`; `role ledger:` states
   `analyzed=200`; cumulative `input_tokens` / `output_tokens` per call against §1 (≈ 900 / 70; if the
   real figure is more than ≈ 25% above the ceiling, stop and revise the budget); no
   `circuit_breaker`; the job finished well inside 90 minutes (record seconds per call).
6. **Label-quality gate (the first look at real labels; nothing before this validates them).**
   *Acceptance rule, set by Alfred on 2026-10-07 and replacing the proposed 40-label / 90% rule:*
   - Alfred reviews **30 pilot labels drawn at random** from the pilot's labels, on a downloaded
     copy of the published snapshot. The draw uses a seed fixed before the pilot runs, so the sample
     cannot be chosen after seeing the labels. The exact command is in
     `docs/planning/SCHEMA_6_ROLLOUT.md`.
   - A label is **correct** when Alfred, reading the headline, agrees with the stored role:
     `principal` if the company is a party to the main development reported, `mentioned` if it is
     only context for someone else's.
   - **Proceed to the bulk backfill only if at least 27 of the 30 are correct.**
   - At 26 or fewer, no bulk dispatch runs. Change the prompt as a **new** prompt version (new
     contract, new rows; the pilot's labels stay as history), repeat the pilot from step 5, and
     review a fresh random 30.
   - Record the count and the date in `docs/planning/WORKSTREAMS.md` either way.

   Separately, and not part of the pass rule: if the pilot happened to label them, look at the two
   MR-003 sessions that were not principal-driven (NVDA 2026-03-23 and 2026-04-21) and the
   Sanofi-reprimand session (PFE 2026-02-03), and note what the labels say. The pilot labels the
   200 highest-priority articles in a fixed order, so these may not be among them.
7. **Bulk backfill.** Dispatches of `max_backfill_roles ≤ 1000` until the report shows
   `deferred_backfill=0` for each ticker. Priority articles go first, so a partial backfill is still
   useful. *After each:* green run, integrity gate and checkpoint passed, tokens within the plan.
   Stop on any `provider_unavailable` or `circuit_breaker` and investigate before resuming.
8. **Steady state.** Change the three workflow fallback defaults from `'0'` to the approved values
   (`max_new_roles` 10, `max_new_roles_total` 25; `max_backfill_roles` stays `'0'`) in a reviewed
   commit; scheduled runs have no dispatch inputs and use those fallbacks. *After the first scheduled
   run:* new articles are labelled within one cycle; `deferred_new` returns to 0.
9. **Outcome-blind selection and the freeze decision.** On the labelled corpus, compute under each
   placement and unlabelled policy: the pooled thresholds, the event funnel (counts only), and history
   sufficiency for NVDA and PFE; write them down *before any return is loaded*, then apply the §3
   decision rule. Only then run the `mr-v1` validation. `MR_V1_FROZEN_THRESHOLDS` stays unset until
   that is approved.
10. **Rollback.** Set all role caps to `0` (the stage becomes inert immediately); the labels already
    stored stay valid and are never edited. If the schema must be undone, restore the rolling `.bak`
    (or the dated copy from step 3) to `state/marketsentinel.db`; an older build opening a v6 file
    ignores the extra table.

### Assumptions this proposal rests on and cannot check offline

- Token sizes use ≈ 4 characters per token; the snippet length and SDK schema overhead are assumed.
- Call latency (hence the 1,000-per-dispatch chunk) is unmeasured.
- Real label quality is unknown: every test uses a scripted provider.
- Live article volume is extrapolated from the MR-003 monthly counts.
- The deployment behaviour in §2 is read from the code (`public_snapshot.py`,
  `publish_public_snapshot.py`, `coverage.yml`); I did not exercise Render or R2.
