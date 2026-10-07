# MarketSentinel Decisions

The single record of settled product, methodology and architecture decisions. Entries are
binding and should survive individual chats.

Append-only, in date order. A later entry may supersede an earlier one; the earlier one stays.
It is not a project diary: implementation history belongs in Git, and ordinary implementation
choices do not belong here.

---

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

## 2026-09-13 — Shared public requests for coverage and analysis

MarketSentinel is a shared public intelligence system across the supported S&P 500 + FTSE 100
universe: no accounts, everyone sees the same data, and any supported company can become covered.

A public user may *request* shared coverage of a company or the analysis of a stored, unanalysed
article. A request is global, anonymous, and idempotent; once processed, the result is published
for everyone through the ordinary snapshot.

The public deployment never spends and never generates. It records a request as one small object
in a dedicated R2 bucket -- the only durable state, because the public host's disk is ephemeral --
using a credential scoped to that bucket alone. The private scheduled worker remains the single
writer and the only holder of an LLM credential.

Spend is bounded on the worker by fixed per-run caps (companies newly activated, article requests
processed, paid attempts across all tickers, tickers cycled), all workflow inputs with defaults.
Public-side caps (pending queues, covered-company ceiling, requests per minute) bound *asking*,
never spend. A request that a capped run does not reach stays queued; consumed requests are
deleted only after the private checkpoint.

Deferred deliberately: accounts, watchlists, per-user state, queue position or ETA, notifications,
failed-analysis status in the UI, and any queue service or worker beyond GitHub Actions and R2.

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

---

## 2026-09-15 — Historical Market Reaction replaces “strongest sentiment correlation”

**Decision:** Build `mr-v1` as a conditional historical market-reaction/event-study layer rather than selecting the strongest sentiment/forward-return correlation across many horizons.

**Primary framing:** clearly positive/negative news sessions -> benchmark-adjusted historical market reaction.

**Primary horizon:** fixed +5 trading sessions. Display day 0 through +10 descriptively. Day 0 is contemporaneous, not predictive.

**Reason:** avoids horizon cherry-picking, matches the user's conditional question, supports honest null results, and creates reusable event/session primitives for later Event-Type Impact and Historical Analogues.

**Important constraints:** leakage-safe session alignment, adjusted prices, market benchmarks, uncertainty, no causal/predictive language, no paid LLM requirement for the core historical analysis.

**Amendments to lead design:**
- mean/median sign disagreement cannot yield `detected`;
- extreme returns are flagged/validated, not automatically removed by a numeric cutoff;
- null copy is “No consistent subsequent move detected,” not “priced in by the close”;
- placebo false-positive behavior must be measured, not assumed;
- numeric `tau` becomes immutable once frozen for `mr-v1`.

---

## 2026-09-15 — Lightweight multi-agent operating model

**Decision:** Move MarketSentinel from serial chat-driven implementation to durable repo work packets and bounded parallel agents.

**Operating model:**
- Alfred owns product direction, consequential decisions, integration and deploys;
- strongest Claude/Fable is reserved for difficult methodology/architecture gates;
- maximum two simultaneous MarketSentinel implementation agents initially;
- isolated Git worktrees per active workstream;
- Wave 1: MR-001 data readiness + MR-002 quant core in parallel;
- MR-003 real-data validation is sequential and freezes the result contract;
- after validation, MR-004 API/snapshot + MR-005 frontend may run in parallel;
- prefer one scheduled integration window per day over interrupt-driven supervision.

**Git policy:** retain the existing safety rule for now: agents do not commit/push/merge/rebase/reset; Alfred performs Git writes. Reconsider branch-local agent commits only if this becomes a measured throughput bottleneck.

**Reason:** obtain most of the wall-clock speedup of multi-agent development without turning a solo-founder project into a high-overhead pseudo-enterprise process.

---

## 2026-09-19 — `mr-v1` principal methodology review: four amendments

**Context:** principal review of `mr-v1` after MR-001 data readiness and the MR-002 quant core.
All four amendments are approved and binding on `docs/product/HISTORICAL_MARKET_REACTION_V1.md`.
Numeric thresholds are still **not** frozen; MR-003 freezes them.

**1. Asymmetric regime thresholds replace a single symmetric `tau`.**

The interface is now a `RegimeThresholds(negative, positive)` pair. Selection population `E` is the
pooled session signals with `distinct_sources >= 3`.

```text
tau_positive = max(0.20, round(Q0.85(S | E), 2))
tau_negative = max(0.20, round(-Q0.15(S | E), 2))
positive event iff S_t >=  tau_positive
negative event iff S_t <= -tau_negative
```

Quantiles are linear-interpolated; selection consumes sentiment only and never returns. The result
records both thresholds, both observed tail shares, and provisional/frozen state. The floor applies
to each tail independently, and **neither threshold may be loosened to recover event counts.**

*Reason:* company-news sentiment is skewed, so one symmetric threshold silently makes one regime a
far rarer and more extreme event class than the other. Measured on the MR-001 NVDA/PFE fixture, the
procedure gives `tau_positive = 0.44` / `tau_negative = 0.22` with both tails at 15.1%, where the
old symmetric rule gave 23.4% / 6.5%.

**2. Day 0 uses exact-timing events only.**

Session assignment is unchanged. Each session signal and event now carries `date_only_share` and
`timing_class` (`exact` when `date_only_share == 0`, else `lagged`). The day-0 aggregate excludes
every lagged event and exposes its own `n_day0`; horizons +1..+10 and the primary +5 statistic keep
all resolved events. Because the cohorts differ, every path horizon states its own cohort and `n`
explicitly rather than letting a consumer assume they match. Full event provenance is preserved.

*Reason:* the conservative date-only rule shifts an article one session late, so a lagged event's
day-0 move is not the reaction to that news. It remains valid for forward horizons, whose anchor
close is after publication either way.

**3. History sufficiency is three conditions, not one span.**

```text
span     = last_signal_index - first_signal_index  >= 126
density  = N_signal / (span + 1)                   >= 0.50
coverage = N_eligible                              >= 63     (sessions with >=3 distinct sources)
```

All three are required and all three observed values are reported.

*Reason:* span alone admits two dense clusters separated by a year of silence. The MR-001 real
fixture is exactly that shape — span 219 with 40 signal sessions — and correctly becomes
`not_enough_history` under the amended rule. The fixture itself is not relabelled.

**4. Benchmarks are investable trackers, not price indices.**

US -> `SPY`; London -> `CUKX.L`. `^GSPC` / `^FTSE` are retained only as validation/reference inputs
where already present.

*Reason:* a price index drops the benchmark's dividend yield while the stock leg is
dividend-adjusted, biasing every market-adjusted return upward by roughly that yield. A total-return
tracker makes the subtraction like for like.

---

## 2026-10-06 — Amended `mr-v1` spec ratified as `mr-v1`

**Decision:** the four amendments of 2026-09-19 stay under the `mr-v1` label. The amended
`docs/product/HISTORICAL_MARKET_REACTION_V1.md` is the `mr-v1` methodology.

**Reason:** nothing has been frozen or published yet, so this is still design, not a version change.

**Rule from here:** `mr-v1` becomes immutable at freeze. After freeze, a material methodology change
means `mr-v2` and an entry in this file.

---

## 2026-10-06 — MR-003 validation scope, pooling and freeze

**Scope:** MR-003 validates on **NVDA and PFE only**, the two names with enough stored history.

**Known gap, accepted:** the London (LSE) path — XLON calendar alignment and `CUKX.L` benchmarking
on real London-listed articles — is **untested in validation**. No London-listed company has stored
articles. MR-003 must state this in its report rather than imply coverage.

**Backfill:** whether to backfill AAPL/MSFT/AMZN or a London name is decided after MR-003's result
is seen, not before.

**Threshold pooling:** where the spec does not say whether companies failing history sufficiency
belong in the selection population, MR-003 computes both and escalates only if a rounded threshold
differs.

**Freeze:** MR-003 proposes `MR_V1_FROZEN_THRESHOLDS` in its branch. Alfred's approval of that
integration is the freeze; there is no separate step.

**Price persistence:** deferred to MR-004, and it must not slip past MR-004. Prices are fetched on
demand and not stored, so a displayed result cannot currently be reproduced. A public result that
cannot be reproduced is a credibility problem for this product specifically.

---

## 2026-10-06 — Coordinator may perform approved local Git operations

**Decision:** the `/coordinate` session may perform local Git operations — commit, merge into local
`main`, create and remove worktrees and branches — **after Alfred approves each integration**.
Approval is per integration; it does not carry over to the next one.

**Never, for any agent:** push, force-push, rebase, `reset --hard`, or deploy. Those stay with Alfred.

**Workers are unchanged:** a worker in a worktree performs no Git writes.

**Supersedes**, for the coordinator only, the "Git ownership" entry of 2026-08-27 and the Git policy
line of 2026-09-15.

**Reason:** manual committing and merging had become the integration bottleneck, while the
irreversible and outward-facing operations are the ones that need to stay in Alfred's hands.

---

## 2026-10-06 — One decisions file

`docs/decisions/DECISIONS.md` was merged into this file, which is now the only decisions record.

---

## 2026-10-06 — After MR-003: data first, nothing frozen

**Context:** MR-003 reported GO WITH LIMITS (provisional). On NVDA and PFE no regime reaches
`n = 20`, every resolved event is `lagged` so day 0 is empty, and a face-validity sample found
session sentiment is often about the wrong party (6 of 12 sampled events cleanly about the company,
4 mixed, 2 not).

**Decision: data first.** MR-004 (API + snapshot) and MR-005 (frontend) are on hold. No public
market-reaction surface is built until the data underneath it is fit to show.

**Thresholds stay provisional.** Pooling (A), all companies with `>= 3`-source sessions:
`tau_positive = 0.42`, `tau_negative = 0.20` (floor applied). `MR_V1_FROZEN_THRESHOLDS` stays
unset. Nothing is frozen, and positive-regime outcomes remain unmeasured.

---

## 2026-10-06 — Primary-company pre-analysis approved

**Decision:** extend the existing scheduled GitHub Actions analysis worker to record, for each
article, whether the covered company is the **principal subject** of the article or is merely
**mentioned**. Backfill the stored articles once, then analyse every new article, all within fixed
budgets. The `mr-v1` engine filters on this label.

**Constraints that carry over unchanged:**
- extraction and the rule that uses it stay separate: the model records what the article is about;
  the filter is a deterministic downstream rule;
- the label is a stored, versioned extraction and is never fabricated: an article that could not be
  labelled has a typed status, not a guessed role;
- only the private scheduled worker spends; the public deployment never does;
- spend is bounded by fixed per-run and backfill budgets.

**Still to be approved, not decided here:** the numeric budgets, the persistent-schema change the
label needs, the exact filter rule and where it applies, and the wording of the spec amendment
(`mr-v1` currently says it does not require paid LLM analysis). The implementing workstream ends by
proposing all four; nothing is spent and nothing is run against the real database before that
approval.

**Reason:** a market-reaction statistic computed from sentiment about some other company is
misleading in exactly the way this product exists to avoid, and no deterministic title rule
recognises the failure shapes MR-003 found.

---

## 2026-10-06 — Later market-reaction workstreams

Queued, not started, each needing its own packet:

- **GDELT investigation for deeper history.** GDELT has never returned a row; a verdict-capable
  regime needs roughly 24+ months of history and the working source's reach beyond 12 months is
  unknown.
- **Bootstrap small-sample false-alarm fix.** MR-003 measured the percentile-bootstrap interval
  rejecting at about 6–9% on mean-zero noise at `n = 20–30` against a nominal 5%. Changing the
  interval method is a methodology change and needs approval before freeze.

---

## 2026-10-07 — Company-role stage: budget, schema, filter rule and spec amendment approved

**Context:** MR-006 delivered the primary-company (company-role) stage offline with the LLM mocked,
and proposed four items in `docs/research/MR-006-proposal.md`. All four are approved.

**1. Budget.**
- Scope: backfill **NVDA and PFE only** (about 2,852 stored articles). AAPL and MSFT are neither
  activated nor labelled now; revisit after MR-007.
- Pilot dispatch of 200 labels first. Alfred reviews a sample of the pilot labels before any larger
  dispatch; nothing bulk runs before that review.
- Then bulk dispatches of at most 1,000 labels each.
- Steady state: 10 new labels per ticker and 25 in total per run.
- Workflow defaults stay `0` until raised on purpose in a reviewed commit.
- Model `gpt-4o-mini`. List price checked 2026-10-07: $0.15 per 1M input tokens, $0.60 per 1M
  output tokens. On the proposal's token estimates that is about $0.04 for the pilot and well under
  $1 for the NVDA + PFE backfill. The estimates are replaced by measured tokens after the pilot.

**2. Schema.** One new table, `article_company_roles`, and `PRAGMA user_version` 5 → 6. No existing
table is altered. Rollout order: refresh the baked fallback snapshot, disable the schedule, push,
deploy the public build, dispatch one worker run with every role cap at `0`, then re-enable the
schedule. The label table stays in the public snapshot, for reproducibility.

**3. Filter rule.** Placement (a): the filter applies to articles **before session signals are
built**, so session sentiment, the distinct-source count, the selection population `E` and history
sufficiency are all computed on `principal`-labelled articles. An **unlabelled article is
excluded**; it is never treated as `principal` or as `mentioned`. Stored `confidence` is not used
by the rule, and there is no third role.

**Fallback, decided outcome-blind.** After the backfill, and before any return is loaded, compute
thresholds, the event funnel (counts only) and history sufficiency for NVDA and PFE. If placement
(a) leaves both companies history-sufficient, (a) stands. If it does not, use placement (b): signals
unchanged, the filter applied at event qualification. The result of that check is written down
before any return is read.

**Consequence for the provisional thresholds.** Under (a) the provisional `0.42` / `0.20` do not
carry over: both thresholds are re-selected on the labelled pool, from sentiment only. Under (b)
they stand. Either way nothing is frozen yet.

**4. Spec amendment.** `docs/product/HISTORICAL_MARKET_REACTION_V1.md` is amended as proposed:
eligibility additionally requires a stored `principal` role label. `mr-v1` still does not require
Stage A/B/C analysis and still uses the broader sentiment-scored corpus as its population, but it
can no longer be computed for a company without paid labelling: a one-off backfill plus every new
article. It stays `mr-v1` because nothing is frozen.

**Also decided:** labels for demo articles are left as they are; the engine already drops demo data.

**Not validated by any of this:** real label quality. Every test used a scripted provider. The
pilot's label review is the first evidence.

---

## 2026-10-07 — Pilot acceptance rule for company-role labels

**Decision:** the bulk role-label backfill may start only if the 200-label pilot passes this rule.

- Alfred reviews **30 pilot labels drawn at random** from the pilot's labels. The draw uses seed
  `20261007`, fixed before any label exists, so the sample cannot be chosen after seeing the labels.
- A label is **correct** when Alfred, reading the headline, agrees with the stored role: `principal`
  if the company is a party to the main development reported, `mentioned` if it is only context for
  someone else's.
- **At least 27 of the 30 correct:** the backfill proceeds.
- **26 or fewer:** no bulk dispatch. The prompt changes as a new prompt version, the pilot is
  repeated, and a fresh random 30 is reviewed. The earlier labels stay as history.

This replaces the 40-label / 90% rule the MR-006 proposal suggested. The count and date of each
review are recorded in `docs/planning/WORKSTREAMS.md`. The commands are in
`docs/planning/SCHEMA_6_ROLLOUT.md`.

**Limit of the rule:** the pilot labels the 200 highest-priority articles in a fixed order, so the
30 are random within those 200, not across the whole corpus.

---

## 2026-10-07 — Interval method: Student-t becomes the `mr-v1` default

**Context:** MR-008 compared four interval methods for the mean on simulated returns only (no real
return or outcome was loaded). Under the selection rule fixed in its packet, **no candidate
qualified**: none held a false-exclusion rate of at most 5.49% for every shape at `n = 20–50`.

**Decision:** adopt the classical **Student-t interval**, `mean ± t(0.975, n-1) · s / sqrt(n)`, as
the default interval method for `mr-v1`, replacing the percentile bootstrap. It is used at every
horizon, including day 0. It is deterministic and needs no seed.

**This is a choice of the best available method, not a method that met the rule.** Measured
false-exclusion rate on mean-zero simulated returns at `n = 20–50` (8,000 trials per cell):

| shape | Student-t | percentile bootstrap (replaced) |
|---|---|---|
| normal | 4.9–5.3% | 5.7–7.8% |
| heavy-tailed, Student-t(3) | 4.2–4.6% | 6.5–8.0% |
| contaminated mixture | 3.6–4.2% | 7.4–8.4% |
| skewed | 6.4–8.0% | 6.9–9.5% |

Student-t is at or below nominal on symmetric shapes and still above it on skewed returns. It gives
up some power: averaged over shapes and true means of ±1% and ±2%, 46.0% → 39.1% at `n = 20` and
54.6% → 50.4% at `n = 30`.

**Disclosure.** The spec and the methodology text state these measured levels and call the interval
approximate. No exact 95% level is claimed, and no coverage figure is shown that was not measured
for the final procedure.

**Zero-width intervals never count as evidence.** No interval method may report "excludes zero" from
a zero-width interval or from a sample with fewer than two observations or no spread. On main today
the percentile default labels 25 identical returns of 1% as `detected` from the interval
`[0.01, 0.01]`; that is closed for every method, at the interval and again at the point where the
verdict rule reads it.

**Not changed:** thresholds, the minimum event counts, the 0.5% effect floor, the split-half and
mean/median conditions. It stays `mr-v1` because nothing is frozen.

**Still unvalidated:** which simulated shape real market-adjusted returns resemble, and the effect
of dependence between events. Both were deliberately not examined.

---

## 2026-10-07 — After MR-007: Google News backfill to 36 months, date-only timing accepted

**Context:** MR-007 found Google News RSS returns a full page for 30-day windows out to at least 36
months for NVDA and PFE, with 91–98% of entries date-only. GDELT's search API answered HTTP 429 and
could not be measured.

**1. Backfill approved: months 13–36, NVDA and PFE, from Google News RSS.**
- Keep the existing 30-day window regime, so the deeper history is sampled the same way as the 12
  months already stored. No shorter windows.
- Do not re-fetch the 12 months already stored. The backfill script only takes a horizon today, so
  a small start-offset change is to be drafted and approved before anything runs.
- **Order:** it runs only after the schema-6 rollout and the label pilot have both passed.
- Still to be approved with that draft: where the run executes against the live corpus, whether it
  performs any paid Stage A/B/C analysis, and the role-label budget for the new articles (the
  approved role budget covers only the articles stored on 2026-10-07).

**2. Date-only timestamps are accepted for verdicts.** The bulk-file route to real publication
times is not to be scoped.

**3. Verdict wording.** Because nearly every event is assigned to the session after publication,
user-facing results must say that they describe moves **from the day after publication**, not the
same-day reaction. No copy may imply a same-session reaction for these results.

**4. GDELT from GitHub Actions: later, non-blocking.** A check of whether the worker's address is
also rate-limited may be run at some point, as a few spaced requests. Nothing waits on it.

**Reason:** depth is what stands between the current corpus and any verdict; MR-003 found no regime
reaches 20 events on 12 months. Extending the same source in the same way adds depth without mixing
sampling regimes. Timing precision is a known, disclosed limit, not a blocker.
