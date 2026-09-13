# MarketSentinel Decisions

This file records settled decisions that should survive individual chats.

It is not a project diary. Implementation history belongs in Git.

## 2026-08-27 — Product identity

MarketSentinel is primarily an evidence-grounded company-intelligence system for medium- and long-term investors.

It is not primarily a sentiment dashboard, news reader, trading bot, or price-prediction product.

The differentiated loop is:

news → events → evidence → materiality → grouping → Key Developments → persistent risks

## 2026-08-27 — Development priority

Intelligence quality takes priority over feature breadth and cosmetic polish.

New work should primarily improve recall, evidence quality, materiality, grouping/ranking, persistent risk intelligence, or investor comprehension.

Commodity infrastructure should use proven libraries/services where practical rather than being rebuilt for its own sake.

## 2026-08-27 — Extraction and materiality remain separate

LLM event extraction and materiality policy solve different problems.

Structured extraction may use LLMs.

Materiality remains a deterministic, auditable downstream layer unless a future methodology decision explicitly changes this.

Do not casually move materiality into an LLM prompt.

## 2026-08-27 — Evidence semantics

Issuer/company-controlled channels do not count as external corroboration of the issuer's own claim.

Use "external source" rather than "independent source" unless actual independence is established.

Contradictions should remain visible and should not automatically disappear from the product.

## 2026-08-27 — Duplicate coverage

Several reports of one underlying business event should normally become one development.

Additional reporting may strengthen evidence breadth, but duplicated publication volume must not inflate importance simply by producing more rows.

## 2026-08-27 — Evaluation reporting

Raw evaluation metrics are primary.

Known-disagreement-adjusted metrics may be reported as diagnostics, but they must not replace or obscure raw performance.

Current materiality evaluation is an in-sample regression evaluation, not evidence of out-of-sample generalisation.

Negative results and known failure modes should be reported rather than relabelled to improve metrics.

## 2026-08-27 — Frozen evaluation vs live data

A committed labelled evaluation fixture is a frozen research snapshot.

The live MarketSentinel database may continue to collect or analyse additional articles.

A drift check failing because the live corpus moved beyond the labelled fixture is expected behaviour, not automatically a product failure.

## 2026-08-27 — Forecasting claims

Forecasting is supporting research functionality, not the current differentiated product core.

Do not present forecast probabilities as validated trading signals or investment recommendations without appropriate out-of-sample validation and calibration.

## 2026-08-27 — Product ownership

The user/product owner decides:

- product direction;
- priorities;
- scope;
- subjective UX/design choices;
- whether a feature is actually useful.

AI may analyse options and surface trade-offs but should not silently make consequential product decisions.

## 2026-08-27 — Engineering workflow

Use small coherent implementation slices.

For meaningful work:

product/methodology decision when required
→ record durable decision
→ bounded implementation
→ relevant automated validation
→ product owner inspects the real result
→ product owner commits if accepted

Do not require a ChatGPT → Claude → ChatGPT review loop for routine implementation.

Expensive adversarial review is reserved for genuine architecture, methodology, difficult debugging, or milestone decision gates.

## 2026-08-27 — Git ownership

AI coding agents perform no Git writes.

Read-only Git inspection is allowed.

The product owner personally performs commits, pushes, merges and all other repository-history changes.

## 2026-09-13 — Continuous coverage and the analysis job ledger

For actively covered companies, the goal is eventual analysis of every relevant unique article, each paid for once.

Every stored article of an active company carries exactly one ledger state per analysis contract (model + prompt versions + schema version): `pending`, `leased`, `retry_wait`, `analyzed`, `skipped`, `failed`, or `baseline`.

New articles become eligible in the same coverage cycle that ingests them. There are no closed-day buckets and no settle delay.

Only deterministic per-article irrelevance rules may terminate an article as `skipped`. Candidate selection may order work but must never permanently mark an otherwise relevant article as not selected. Budget-limited work stays `pending`.

Ingestion watermarks are stored per (ticker, provider), advance independently, never move backwards, and keep an overlap window for late arrivals.

A stored current-contract analysis is reused despite evidence drift; the ledger records whether its evidence is still current, and regeneration stays the explicit `refresh-evidence` mode.

Private `/api/v1/analyze` and the per-article analysis endpoint spend only through the ledger. Backfill and repair modes remain explicit operator paths outside it.

Activation spends nothing: existing current-contract analyses become `analyzed`, older unanalysed history becomes `baseline`.

The ledger is operational state only. Materiality, grouping, ranking, and risks remain deterministic and recomputed on read.

A provider-cap hit (`partial`) must never advance a watermark past articles it did not return; this is verified against a real smoke-test corpus, not only synthetic fixtures.

`google_news_rss` is windowed into day-sized, date-bounded requests before its result cap is evaluated, so a high-volume ticker converges to `ok` instead of hitting the cap every cycle. This only reduces how often `partial` occurs; the underlying not-advance-on-partial rule above is unchanged, so a single day too dense for the cap still leaves the watermark in place rather than guessing.

Each windowed request is allowed up to Google's own observed per-request ceiling (100 entries, confirmed against the live endpoint and unaffected by the date range asked for), not the smaller interactive-refresh budget: that budget was sized for one flat multi-day request and would otherwise re-impose the same cap on every single day. Google's `after:`/`before:` operators do not accept a time component (confirmed against the live endpoint: a time-bounded query returns nothing), so no window can be split finer than one calendar day. A day genuinely exceeding Google's own ceiling is a hard limit of this data source and still correctly reports `partial`.

## 2026-09-13 — Scheduled coverage and public snapshot publication

Continuous coverage (the analysis job ledger) needed a scheduler and a way to get its results to
the public deployment without ever committing generated data to Git or putting OpenAI credentials
on the public host. The shape:

- **GitHub Actions is the scheduler.** `.github/workflows/coverage.yml` runs on a cron (default
  every 6 hours) plus manual dispatch, under a `concurrency` group so overlapping writers can never
  race the same private database. No self-hosted scheduler, queue, or daemon.
- **Cloudflare R2 is durable object storage**, holding two distinct things:
  - a **private operational SQLite database** (`state/marketsentinel.db`, plus one rolling `.bak`
    generation) -- the live, growing, already-paid-for corpus. Only this workflow has credentials
    for it. Downloaded at the start of each run, and **checkpointed back to R2 immediately after
    the coverage cycle is validated -- before any public snapshot work starts.** This ordering
    matters: the cycle's paid Stage A/B/C analyses exist only on the ephemeral runner's disk until
    that checkpoint durably persists them, so if the checkpoint ran *after* snapshot build/publish
    instead, a failure in that later, unrelated work would strand newly paid analyses on a
    throwaway runner and the next run would re-pay for them. The checkpoint retries each upload
    (bounded exponential backoff) and, like every other gate in this workflow, a failure after
    retries stops the job -- public snapshot build/scan/publish and the Render restart never run
    against work that was not actually saved.
  - a **public snapshot bucket**, written as immutable, content-addressed objects
    (`snapshots/<version>/...`) plus one small mutable `latest.json` manifest carrying each
    asset's URL, sha256, and size. The versioned files upload first; `latest.json` uploads last and
    only on success, so a reader can never observe it pointing at an object that is not there yet,
    and a failed run never touches the previously published snapshot.
- **The private database download fails closed, on purpose.** If `state/marketsentinel.db` is not
  found in R2, the job stops with an explicit error rather than silently starting from an empty
  database -- an empty start would both discard the accumulated corpus and cause the ledger to
  re-pay for analyses it already has on record. This means R2's private bucket must be seeded once,
  manually, before the very first scheduled run (see README.md's Bootstrap steps); after that, the
  workflow's own checkpoint keeps it current with no further manual step. The constituent cache is
  exempt from this: it is free, public, reference-only data with no cost or paid state attached, so
  it alone keeps a soft fallback (`scripts/warm_constituent_cache.py`, live from Wikipedia) when
  missing.
- **OpenAI credentials exist only in GitHub Actions secrets.** The public Render service never
  receives an LLM key and performs no analysis; this is what keeps the read-only deployment's
  attack surface small even though it is fully public.
- **The public deployment fetches its own snapshot at startup.** `marketsentinel.public_snapshot`
  (invoked from the Docker image's `CMD`, before uvicorn starts) downloads `latest.json`, verifies
  both assets' sha256, runs SQLite's own `integrity_check`, and checks `schema_user_version`
  against this build's own schema version -- only then replacing the files the image baked in at
  build time (`deploy/public-snapshot.db`, `deploy/constituents_cache.json`, unchanged from the
  existing manually-committed deployment path). Any failure -- unreachable manifest, hash mismatch,
  corrupt database, schema mismatch -- is caught internally, logged, and leaves the baked-in
  snapshot untouched; the fetch never raises and never blocks uvicorn from starting. This is a
  restart, not a redeploy: the container image itself never changes between coverage runs.
- **No automated Git commits or pushes anywhere in this path.** The workflow does not touch the
  repository's history. The existing `deploy/` snapshot stays a manually reviewed, manually
  committed fallback baseline; R2 is the sole channel for automatically refreshed public data.
- **Validation is layered, not single-point.** The private database's own `integrity_check` gates
  before a snapshot is even built (`scripts/check_database_integrity.py`); the sanitized snapshot
  then goes through the existing `scripts/build_deployment_snapshot.py` machinery unchanged --
  `VACUUM INTO`, its own `integrity_check`, and the full credential/personal-data scan -- before
  `scripts/publish_public_snapshot.py` will write a manifest at all. A failure at any of these
  points fails the GitHub Actions job, which skips every step after it, including the Render
  restart -- so the last good public snapshot is never replaced by a bad or partial one.
- **Initial cadence is 6 hours and 25 new analyses per ticker per run**, both `workflow_dispatch`
  inputs with defaults, changeable without touching any Python.
