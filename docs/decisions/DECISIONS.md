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
