# MarketSentinel Architecture

Derived from the current codebase. Describes what exists, not what might be built.

Product intent lives in [PRODUCT.md](../product/PRODUCT.md); settled decisions in
[DECISIONS.md](../decisions/DECISIONS.md); the session entrypoint is [CLAUDE.md](../../CLAUDE.md).

## Shape of the system

One API over one SQLite file, with two clients that serve different purposes:

- **API** — FastAPI ([api/app.py](../../src/marketsentinel/api/app.py)). `build_services` wires
  every concrete provider once; `create_app` exposes the eight routes tabulated below. Allowed
  browser origins come from `cors_allow_origins` in
  [config.py](../../src/marketsentinel/config.py) rather than being hardcoded. `GZipMiddleware`
  compresses responses over 1 KB — transport only, no contract change, and it matters because one
  company's uncapped article list is several hundred KB of repetitive JSON.
- **React client** ([frontend/](../../frontend)) — Vite + TypeScript + Recharts. The
  recruiter-facing read surface. **GET-only: it contains no write client at all**, so it cannot
  refresh coverage or request a paid analysis even when pointed at a private API.
- **Streamlit dashboard** ([dashboard.py](../../src/marketsentinel/dashboard.py)) — the
  private/local operational surface, and the only client that can trigger work: the explicit
  company refresh and the manual per-article analysis. A pure HTTP client like the React one; it
  holds no database handle and calls no provider.

Both clients read the same conclusions from the same endpoints. Neither re-derives materiality,
grouping, ranking, or corroboration.

| Method | Route | Reads | Spends |
| --- | --- | --- | --- |
| GET | `/health` | — | no |
| GET | `/api/v1/capabilities` | deployment mode, prepared set, stored-article counts | no |
| GET | `/api/v1/constituents/search` | the constituent universe | no |
| GET | `/api/v1/companies/{symbol}/overview` | the `CompanyOverview` projection | no |
| GET | `/api/v1/companies/{symbol}/articles` | stored scored articles (Relevant News) | no |
| GET | `/api/v1/companies/{symbol}/articles/{article_id}/analysis` | one stored analysis | no |
| POST | `/api/v1/analyze` | refreshes coverage | **yes** |
| POST | `/api/v1/articles/analyze` | generates one analysis | **yes** |

Domain contracts are Pydantic models in [domain.py](../../src/marketsentinel/domain.py), which is
also where the two projections live: `ArticleAnalysis.to_analyzed_event()` (chart markers) and
`.to_company_intelligence_event()` (the product-facing record every downstream layer reads).

## Deployment mode: public and private

`public_mode` in [config.py](../../src/marketsentinel/config.py) (default `False`) is the single
switch separating a published read-only deployment from a local operational one. Its **only**
enforcement is closing the two rows marked *spends* above: both return `404`, so the surface does
not exist rather than advertising a disabled capability. Everything else is identical in both
modes.

That enforcement lives at the API boundary and never in a client, because the API is reachable
independently of any frontend — a hidden button restricts nothing.

- **Search and reads stay open across the full constituent universe in both modes.** A public
  deployment is genuinely searchable; it does not pretend the index is two companies wide.
- **`public_prepared_companies`** (default `("NVDA", "PFE")`) is an editorial *label* for the
  deliberately backfilled deep demonstrations — not an allowlist, and deliberately not a computed
  quality threshold. Which companies count as prepared is a product decision, not something the
  application infers from its own stored counts.
- **`/api/v1/capabilities`** reports `mode`, `default_symbol`, `prepared_companies`, and
  `coverage`: the raw stored-article count per ticker from `repository.stored_article_counts()`
  (one `GROUP BY ticker`, demo rows excluded). A client joins these to label search results —
  *Prepared coverage · N articles*, *N stored articles*, or *No stored coverage* — without
  reordering or filtering what the server returned. The response is advisory: the POST
  restrictions it describes are enforced above regardless of what a client does with it.

A company the universe contains but the database does not is served as a real, empty
`CompanyOverview`, not a 404. A 404 from a read endpoint means the symbol is not a constituent at
all.

## The pipeline

`MarketAnalysisService.analyze(symbol)` in [service.py](../../src/marketsentinel/service.py) is
the whole live path, in order.

### 1. Ingest and store

[sources/historical.py](../../src/marketsentinel/sources/historical.py) (GDELT DOC 2.0, with a
date-bounded Google News RSS fallback) plus
[sources/news.py](../../src/marketsentinel/sources/news.py) (recent Google News RSS, with an
explicitly labelled demo fallback) return articles.
[normalization.py](../../src/marketsentinel/normalization.py) assigns a fingerprint and a relevance
score, and deduplicates deterministically — provider ID, canonical URL, or identical normalized
title plus publisher within six hours. Never fuzzy matching. Survivors are upserted into
`articles`; unscored ones are FinBERT-scored
([sentiment/finbert.py](../../src/marketsentinel/sentiment/finbert.py)) and aggregated into
`daily_sentiment` ([aggregation/sentiment.py](../../src/marketsentinel/aggregation/sentiment.py)).

An `IngestionFunnel` counts every drop reason, so a small article count reads as a verdict rather
than as missing data.

### 2. Candidate selection

[analysis_candidates.py](../../src/marketsentinel/analysis_candidates.py) decides which stored
articles are worth a paid analysis. Pure: no I/O, no implicit clock. It rejects demo rows,
low-relevance rows, third-party holding reports, price predictions, market-reaction-only headlines,
and routine Rule 10b5-1 sales; ranks by source tier then recency (optionally promoting disclosure
and corporate-action vocabulary *within* a tier, never above one); then admits under a publisher
cap, an official-company cap, and near-title deduplication. `priority_bonus_limit` allows a small
number of extra accepts drawn only from priority-signal articles the ordinary walk did not reach,
so a quiet month stays cheap without capping a dense one at a quiet month's budget.

`AutomaticAnalysisDiagnostics` records each rejection count.

### 3. Stage A / B / C

[event_analysis.py](../../src/marketsentinel/event_analysis.py). `ArticleEventAnalysisService`
builds a deterministic `_AnalysisContext` first — subject company, source class, ranked evidence
pool, related-company candidates, evidence fingerprint — then runs three typed provider calls
through `OpenAIArticleIntelligenceProvider`, or `UnavailableArticleAnalysisProvider` when no key is
configured, which fails safely rather than degrading.

| Stage | Version constant | Produces |
| --- | --- | --- |
| A — event extraction | `event-extraction-v7` | type, direction, magnitude, confidence, horizon, important claims, positive/negative transmission channels |
| B — claim/evidence | `claim-evidence-v1` | per claim: corroborated / contradicted / unsupported / uncertain, plus the article IDs cited |
| C — related companies | `related-company-v5` | related-company effects, only from supplied candidates, only where a concrete event-specific mechanism exists |

Guarantees enforced in code, not only in the prompt:

- Article fields are wrapped in `_untrusted_input(...)` and are never instructions.
- Output is parsed into Pydantic drafts; a structural failure raises
  `ArticleAnalysisStructuralValidationError`.
- Stage B assessments must reference a supplied claim ID and only supplied evidence IDs; a
  *corroborated* or *contradicted* verdict citing nothing is rejected as
  `ArticleAnalysisSemanticValidationError`.
- Stage C tickers must come from the supplied candidate list and may never be the subject company.
- `_normalise_external_institutional_holding` stops a third party's stake change becoming a
  subject-company transaction; `_validated_regulation_event` stops `regulation` being asserted
  without a genuine state actor in the supplied record.
- Any failure returns a typed `ArticleAnalysisResponse`. Nothing is fabricated.

**Evidence pool.** `_evidence_candidates` selects contemporaneous coverage in a `-30 / +14` day
window around the primary's own publication date, sorted by proximity before the cap, so a wide
window never depends on database row order and later unrelated coverage cannot crowd out
contemporaneous reporting. `_rank_evidence` then picks the top *n* (default 5), preferring distinct
publishers. `_evidence_strength` measures the breadth of supplied context, not the probability that
a claim is true.

**Automatic analysis budget.** `service._run_automatic_analysis` walks the candidates, stops after
`analysis_auto_max_new_per_run` new attempts, and trips a circuit breaker after two consecutive
failures. Cached hits are free and never count against the budget. Setting the cap to `0` is a kill
switch that leaves the manual per-article endpoint working.

Both private spending endpoints call their runner through the analysis job ledger (see
[Continuous coverage](#continuous-coverage-the-analysis-job-ledger)): `build_services` wires a
`LedgeredArticleAnalysisRunner` in front of `ArticleEventAnalysisService`, so `/analyze` and the
per-article endpoint lease a job before paying, record the attempt, and reuse a stored
current-contract analysis instead of paying again when only the evidence pool has grown.

### 4. Persistence and versioning

[storage/sqlite.py](../../src/marketsentinel/storage/sqlite.py) is the only persistence layer.
Tables: `articles`, `sentiments`, `daily_sentiment`, `article_intelligence_analyses`, plus the
operational coverage ledger `company_coverage`, `ingestion_watermarks`, `article_analysis_jobs`
(`PRAGMA user_version = 5`, WAL, additive tables and column migrations applied at `initialize()`).
The ledger holds operational state only — never a materiality verdict, group, rank, or risk.

An analysis row is keyed by `(article_fingerprint, model_version, cache_version, schema_version)`
and inserted `ON CONFLICT DO NOTHING` — **append-only; a prior version's payload is never
overwritten**. `cache_version` is composed at runtime as
`a=<stageA>;b=<stageB>;c=<stageC>;e=<evidence_fingerprint>`, so re-running against a changed
evidence pool produces a new row rather than a silent overwrite.

[analysis_compatibility.py](../../src/marketsentinel/analysis_compatibility.py) is the single
contract governing reuse:

- `accepts_for_display` — all four prompt/schema versions match exactly. Anything else is skipped
  by `list_article_analyses` and never rendered.
- `accepts_for_cache` — display compatibility, *plus* a matching model version, *plus* a matching
  evidence fingerprint. Only then may a stored result replace a paid call.

Exact equality, never a range. Bumping any version constant retires the entire stored corpus for
both display and cache purposes.

### 5. Materiality, grouping, ranking

[materiality.py](../../src/marketsentinel/materiality.py). Deterministic, auditable, and **nothing
here is persisted** — every verdict recomputes from stored analyses per request, so a policy change
needs no migration and no re-analysis.

`assess_materiality` applies four conditions in order and records the first failure by name:

1. **guard** — not commentary, advocacy, or a preview (`reads_as_commentary`); not a share-price or
   market-value move (`describes_market_move`); not a percentage price move
   (`describes_percent_price_move`, deliberately local to this module because selection must still
   admit price-reaction articles as candidates); and not an article in which the subject company is
   not a principal (`subject_principal.reads_as_third_party_appointment`, the only guard that reads
   which company is under analysis);
2. **driver** — passes the shared meaningful-event floor and names an identifiable cash-flow or
   risk driver. An `other` row carrying disclosure vocabulary is rescued to tier 1;
3. **durability** — an inherently durable event type, a rescued disclosure, or a horizon of weeks
   or longer;
4. **evidence** — a qualifying source class (filing or major financial wire), external support, or
   a first-hand issuer disclosure. An issuer is *not* evidence for the editorially-prone types:
   product launch, partnership, other.

A *contradicted* claim never rejects a row; the dispute is attached and displayed.

`describes_same_material_event` merges two reports of one development through two arms: heavy title
overlap (≥ 0.75, syndication) needs nothing further; otherwise the pair must agree on event type
and direction, reach ≥ 0.30 overlap, and share at least one `anchor_terms` stem — capitalised or
digit-bearing tokens, with the subject company removed. Every pair comparison is additionally
bounded by a 96-hour publication window.

`_group_assessed` then applies that predicate greedily in a single pass over the material events,
in input order: an event joins the **first** existing group containing any member it matches, and
otherwise starts a new group. Existing groups are never merged afterwards, so a later event that
matches members of two different groups joins only the first and does not bridge them. Grouping is
therefore deterministic for a given input order, but it is not a transitive closure.

`prepare_key_developments` orders groups **lexicographically, never blended**: magnitude, then
event-class tier, then external-source count, then publisher count, then recency, then article ID.
`MaterialityDiagnostics` reconciles exactly (`considered` = material + every rejection) and
`key_developments_caption` states the funnel, so a display limit never reads as an absence.

### 6. Persistent Top Risks

Three modules, all deterministic and unpersisted:

- [risk_taxonomy.py](../../src/marketsentinel/risk_taxonomy.py) — a fixed `RiskTheme` set reached
  through ordered, boundary-aware regex triggers. No embeddings, no fuzzy matching, no LLM
  classifier. Unrecognised mechanisms become `UNMAPPED` and are never ranked.
- [risk_signals.py](../../src/marketsentinel/risk_signals.py) — a signal exists only where a stored
  analysis passes the shared meaningful-event floor, reports something the subject company is a
  principal to (the same `subject_principal` rule the materiality gate reads), and either
  *realised* a negative event with a safe theme mapping, or stated an explicit negative
  transmission channel (damped by direction). Downside is never inferred from magnitude, from
  positivity, from price reaction, or from source prominence.
- [risk_scoring.py](../../src/marketsentinel/risk_scoring.py) — `rank_company_risks` takes the
  **strongest** signal per theme rather than a sum, so repeated reporting cannot inflate a theme.
  Publisher and event-group bonuses are each capped at 0.04 × 2, so even total grouping failure can
  move a score by at most 0.16. The severe band (> 69) additionally requires more than a single
  general-news article.

`concern_index` is an evidence-weighted salience score on 0–100. It is not a probability, an
expected loss, or a cross-company comparable.

### 7. The Company Overview projection

[overview.py](../../src/marketsentinel/overview.py) assembles one typed `CompanyOverview` from the
layers above. It owns no threshold, ordering, gate, or vocabulary of its own: every verdict, rank,
count and label is produced by the module that already owns it, so the projection cannot hold a
second opinion about what a development is. Its purpose is to move those call sites from a client
process to the API boundary, so a non-Python client reads the product's conclusions instead of
re-deriving them.

`MarketAnalysisService.read_stored` serves it over `GET /api/v1/companies/{symbol}/overview`. That
path fetches no news, scores no sentiment, runs no article analysis, and writes no row — it reads
the SQLite corpus, recomputes the deterministic layers, and resolves the constituent from the local
cache only, so opening the product costs neither ingestion nor LLM spend. Refreshing coverage stays
an explicit `POST /api/v1/analyze`.

Price history is the one exception, because it is not persisted. Two bounds keep that honest and
cheap on a broadly searchable public deployment:

- **Zero-coverage reads fetch nothing.** `read_stored` reads the stored corpus *first*; if a
  company has no articles, no sentiment, and no analyses, the price fetch is skipped entirely and
  the chart is reported `unavailable` with `NO_STORED_COVERAGE_PRICE_MESSAGE`. The page renders an
  intentional empty state that shows no chart, so the fetch would be the read's only external call,
  made purely to be discarded — once per visitor per empty company across a ~600-company universe.
- **`CachingPriceProvider`** ([sources/prices.py](../../src/marketsentinel/sources/prices.py))
  wraps the live provider with a per-process, in-memory, per-symbol TTL
  (`price_cache_ttl_seconds`, default 900). No database column, no migration, no external cache; a
  restart simply starts cold. Only successes are cached, so a transient outage cannot be pinned in
  place for the whole TTL. A failed fetch still reports an unavailable chart rather than
  substituting a series.

Two further read-only projections sit beside the overview, each with its own endpoint rather than
being bundled into that payload:

- **Relevant News** (`/articles`) — every stored, sentiment-scored article inside a 366-day window,
  assembled by [dashboard_articles.py](../../src/marketsentinel/dashboard_articles.py) and
  `overview.build_relevant_news`. The window is the *only* bound: there is deliberately no row cap,
  because a cap on top of a bounded window drops real rows while presenting the truncated page as
  the whole corpus. Each row carries `has_compatible_analysis`, which reuses the same
  `accepts_for_display` test applied everywhere else — never a looser notion of "analysed".
- **One stored analysis** (`/articles/{article_id}/analysis`) — `read_article_analysis` returns an
  already-stored record through `prepare_intelligence_card`, the same function behind Today's
  Intelligence, so an analysis opened from the article browser is described exactly as it would be
  anywhere else. It never *generates*: an article with no compatible stored analysis is a 404, not
  an offer to create one, which is why it is safe on a public deployment.

### 8. Presentation

Both clients render conclusions the server already reached. The `dashboard_*` modules are the
shared preparation layer: pure, payload-only, and reachable from either the Streamlit process
(directly) or the React client (through the typed projections in
[overview.py](../../src/marketsentinel/overview.py)).

**The React client re-derives none of it.** [api/types.ts](../../frontend/src/api/types.ts) mirrors
`domain.py` field for field and adds nothing. Which rows are developments and in what order, which
reports are one development, which stored analyses may carry a chart marker, what may be called
external support, and every impact/tier/corroboration label all arrive already decided; the client
maps them to elements. What it *does* compute is confined to presentation with no product
judgement in it: marker ordinals from the server's own marker order, client-side date/source/
sentiment/analysed filtering over the already-returned article rows, and the coverage labels
described above from server-supplied counts. It applies no threshold, no ranking, and no gate.

Two deliberate divergences from the Streamlit surface, both display-only:

- The pane titled **Top intelligence** is the `todays_intelligence` field. `prepare_todays_intelligence`
  applies no date filter — it ranks every eligible stored analysis by magnitude, confidence,
  evidence strength, source class, then time, and takes the strongest few — so the React label
  states what the ranking actually is. The API field and function names are unchanged.
- The price/sentiment chart encodes the two series differently rather than as two similar lines:
  price is a continuous line, sentiment a diverging bar per observed day. Chart rows are the union
  of price dates, sentiment dates, and marker dates, and sentiment is scored on calendar days while
  price exists only on trading days; the price line therefore connects across the resulting
  weekend gaps. It displays no value that was not observed — no dot is drawn on a null and the
  tooltip still gates each series on its own presence.

The pure preparation modules:

- [dashboard_articles.py](../../src/marketsentinel/dashboard_articles.py) — pairs each stored
  article with its compatibility flag for the Relevant News browser. Owns no rule: "analysed" is
  the existing exact-equality compatibility test, applied by the repository read.

- [dashboard_intelligence.py](../../src/marketsentinel/dashboard_intelligence.py) — Today's
  Intelligence, plus **the corroboration semantics the whole product shares**. Only articles cited
  by a *corroborated* claim count; they are reduced to distinct publishing organisations with the
  primary's own organisation and syndicated rewrites of it removed. The result is called *external*
  support, never *independent*, because nothing in the stored data establishes independence.
  `materiality.py` imports this rather than re-deriving it.
- [dashboard_risks.py](../../src/marketsentinel/dashboard_risks.py),
  [dashboard_charts.py](../../src/marketsentinel/dashboard_charts.py),
  [dashboard_market_view.py](../../src/marketsentinel/dashboard_market_view.py) — which computes
  each observation independently and deliberately never fuses them into an overall score, verdict,
  or stance — and
  [dashboard_event_state.py](../../src/marketsentinel/dashboard_event_state.py), typed marker
  updates that reject incompatible legacy payloads.

Every `compatible_*` helper re-validates against the current Pydantic contract and *drops*
incompatible records rather than filling in missing fields.

## Differentiated core vs supporting infrastructure

**Differentiated core** — where the product's value and most of its care live:

[analysis_candidates.py](../../src/marketsentinel/analysis_candidates.py),
[event_analysis.py](../../src/marketsentinel/event_analysis.py),
[event_policy.py](../../src/marketsentinel/event_policy.py),
[analysis_compatibility.py](../../src/marketsentinel/analysis_compatibility.py),
[materiality.py](../../src/marketsentinel/materiality.py), the three risk modules,
[dashboard_intelligence.py](../../src/marketsentinel/dashboard_intelligence.py) for corroboration,
and the frozen gold fixtures with
[scripts/evaluate_materiality.py](../../scripts/evaluate_materiality.py).

**Supporting infrastructure** — necessary, but not the USP; prefer proven libraries over rebuilding:

`sources/*` (news, GDELT, prices), `normalization.py`, `storage/sqlite.py`, `sentiment/finbert.py`,
`aggregation/sentiment.py`, `forecasting/baseline.py` (an explicitly experimental five-session
direction diagnostic, never a validated trading signal), `constituents.py`, `config.py`,
`dashboard_charts.py`, and the FastAPI, Streamlit, and React boundaries themselves — including the
React client's presentation choices, which are deliberately not where the product's value lives.

## Backfill: a manual, one-shot path

[backfill_service.py](../../src/marketsentinel/backfill_service.py), plus the pure planner
[historical_backfill.py](../../src/marketsentinel/historical_backfill.py), driven only by
[scripts/backfill_historical_intelligence.py](../../scripts/backfill_historical_intelligence.py).
No scheduler, queue, or background worker.

It reuses the live pipeline's own building blocks — candidate selection, the `ArticleAnalysisRunner`
protocol, sentiment aggregation — over disjoint calendar-month buckets under an explicit run-level
budget, and never alters the live `/analyze` funnel, its caps, or the compatibility rule. It only
produces more data for that unchanged machinery to read. Modes: `backfill`, `reanalyze-stale`,
`refresh-evidence`, `fill-selection-gaps`. A partial or failed month is reported per bucket, never
presented as complete.

Backfill and its repair modes call `ArticleEventAnalysisService` directly, not through the job
ledger. They are explicit operator actions with their own budgets; `refresh-evidence` in particular
must still regenerate an analysis whose evidence changed. Articles they store are picked up by the
next coverage cycle's reconcile step like any other stored article.

**Historical backfill/repair and continuous coverage must not run concurrently for the same
ticker.** The legacy backfill path takes no ledger lease, so a backfill or repair run overlapping a
coverage cycle (or a private `/api/v1/analyze` refresh) can pay for the same article twice. The
ledger's pre-call check for a stored analysis narrows but does not close that window. Both CLIs
state this in their help, and the backfill CLI prints it as a runtime warning — emphasised when the
ticker is under continuous coverage. Wrapping backfill in the ledger was deliberately not done, to
avoid silently changing its explicit refresh and reanalysis semantics.

## Continuous coverage: the analysis job ledger

[coverage_cycle.py](../../src/marketsentinel/coverage_cycle.py) and
[analysis_ledger.py](../../src/marketsentinel/analysis_ledger.py), driven by
[scripts/run_coverage_cycle.py](../../scripts/run_coverage_cycle.py). Synchronous and one-shot, like
backfill: no scheduler, queue, or daemon — repetition is scheduled externally. Its goal is that
every relevant unique article of an actively covered company is eventually analysed once, and that
every stored article carries one explicit, inspectable state.

**Keys.**

| Purpose | Key |
| --- | --- |
| Job | `(article_fingerprint, analysis_contract)`; the contract is `ArticleAnalysisCompatibility.contract_key` = model + A/B/C prompt versions + schema version, deliberately *without* the evidence fingerprint |
| Watermark | `(ticker, provider)` → `ingested_through` |
| Coverage | `ticker` → `active`, `ledger_started_at`, `live_window_days` |

**Job states.** `pending`, `leased`, `retry_wait` are active; `analyzed`, `skipped`, `failed`,
`baseline` are terminal and never left by an ordinary transition. The one exception: a stored
current-contract analysis always completes a job as `analyzed`, because the stored analysis is what
the ledger describes. An explicit per-article request (`POST /api/v1/articles/analyze`) may retry a
terminal job; automatic work never does.

- `skipped` comes **only** from per-article irrelevance rules the selector already applies — demo,
  low relevance, third-party holding, price prediction, market-reaction-only, routine 10b5-1 sale.
  `deterministic_skip_reason` obtains them by running the unchanged selector over the single
  article, where no relative cap can bind.
- Publisher cap, official-company cap, near-title, and limit are relative to other articles, so they
  only set **processing order** (`processing_order`: the selector's ranked admits first, then the
  rest newest first). Budget-limited work stays `pending`; nothing relevant becomes terminal because
  of a budget.
- `baseline` marks stored history older than `ledger_started_at − live_window_days` without a
  current-contract analysis. Activation creates it and spends nothing.

**One cycle per ticker.**

1. *Ingest* — each provider (`gdelt`, `google_news_rss`) fetches independently from its own
   watermark minus a 48-hour overlap, clamped to its lookback (a clamp is reported as a gap).
   `google_news_rss` (`RecentProviderSource`) additionally splits that span into day-sized,
   date-bounded requests (`_fetch_windows`) and merges the results (`_merge_fetch_results`) before
   the rest of ingest sees them: Google's RSS search enforces its own result cap per request with
   no cursor to page past it, so one flat request over a high-volume week hits that cap every
   cycle, while day-sized requests each stay comfortably under it for a normal news day. Google's
   date operators (`after:`/`before:`) are day-granularity only -- a time component makes Google
   return nothing, checked directly against the live endpoint -- so a window cannot be split any
   finer than one day this way. Each window's own request is allowed up to
   `GOOGLE_NEWS_RSS_RESULT_CAP` (100, Google's own observed per-request ceiling regardless of the
   date range asked for) rather than the smaller interactive-refresh `max_articles` budget, which
   was sized for one flat request over the whole span and would otherwise silently re-impose the
   same cap per day. A merged result carries through any one window's failure or cap hit exactly
   as an unwindowed fetch would, so the caller cannot tell the two apart, and a single day genuinely
   denser than Google's own ceiling still correctly stays `partial` -- windowing raises how much a
   dense day can converge, it does not remove the underlying per-request ceiling. New articles are
   deduplicated against stored
   rows, upserted, FinBERT-scored, and daily sentiment is recomputed, all *before* any watermark
   moves. Only an `ok` or `empty` fetch advances a watermark. A `failed` fetch, and a `partial`
   fetch that hit the provider's article cap (now only when even a single day is too dense to
   fit), leave it in place: the articles a capped fetch returned are stored, and the next cycle
   retries the unresolved window from the previous watermark instead of skipping the older
   articles the cap cut off. `consecutive_failures` counts consecutive `failed` *and* `partial`
   fetches and resets on `ok`/`empty`, so a window a provider cannot cover within its cap stays
   visible rather than silently retried forever. Watermarks never move backwards.
2. *Reconcile* — every stored article without a job gets exactly one: `analyzed/preexisting`,
   `skipped/<rule>`, `baseline`, or `pending/eligible`. This also covers articles stored by
   `/analyze` or backfill, and a crash between storing and enqueueing.
3. *Analyse* — claimable jobs (pending, due retries, expired leases) run through the runner in
   processing order under `--max-new` paid attempts and the existing two-consecutive-failure
   breaker. An unconfigured provider stops the pass without consuming an attempt.
4. *Check evidence* — for analysed jobs never checked, or recent enough that their evidence window
   may still be filling, compare the newest stored analysis's evidence fingerprint with the one a
   fresh analysis would receive (`current_evidence_fingerprint`, no provider call) and record
   `evidence_current`. A changed pool is reported; regenerating it stays the explicit
   `refresh-evidence` backfill mode.

**Idempotency and retries.** Job creation is `INSERT … ON CONFLICT DO NOTHING`. A job is leased by a
single compare-and-set UPDATE (10-minute lease) before any paid call; reclaiming an expired lease
counts the abandoned attempt as `lease_expired`. The runner first checks for a stored
current-contract analysis, so a crash after the analysis was stored recovers without paying.
Attempts, failures, and summed per-stage token usage are added unconditionally when an attempt
finishes; the state change is conditional on still holding the lease. Failure categories come from
`ArticleAnalysisResponse.failure_category`: `timeout`, `transport_error`, `http_error`, and
`lease_expired` retry at 15 min / 1 h / 4 h up to 4 attempts; `pydantic_validation`,
`semantic_validation`, and `unexpected` retry once; anything else fails permanently; `demo` is
`skipped`.

**Contract-version bumps.** Changing the model, a prompt version, or the schema version creates a
new `analysis_contract`; old-contract job rows stay as history. The next reconcile applies the same
rules under the new contract, with the ticker's *original* horizon
(`ledger_started_at − live_window_days`): articles published on or after it become `pending` and
are re-analysed by later cycles (real spend), while older stored history becomes `baseline` and is
**not** enqueued automatically. Re-analysing that pre-activation history still requires an explicit
backfill mode such as `reanalyze-stale` (or a deliberate requeue).

**Evidence reuse.** `accepts_for_contract` is display compatibility plus model version, without the
evidence fingerprint. It is a *spending* rule used only by the ledger; `accepts_for_cache` and
`accepts_for_display` are unchanged, and the display path still shows the newest compatible
analysis.

## Scheduled coverage and public snapshot publication

[.github/workflows/coverage.yml](../../.github/workflows/coverage.yml) is the scheduler for
continuous coverage: a cron (default every 6 hours) plus manual dispatch, under a `concurrency`
group so two runs never race the same private database. It is the only place OpenAI credentials
exist; the public Render deployment never receives one and performs no analysis.

One run, in order: **download the private operational database from R2, failing the job closed if
it is missing** (see below -- this is not a best-effort start-from-empty); warm the constituent
cache from Wikipedia only if it alone is missing
([scripts/warm_constituent_cache.py](../../scripts/warm_constituent_cache.py), so the offline-only
`--mode activate` step that follows it can stay offline as designed); activate (idempotent); run a
bounded `--mode cycle` (`scripts/run_coverage_cycle.py`) under `--max-new`; validate the resulting
database with `PRAGMA integrity_check`
([scripts/check_database_integrity.py](../../scripts/check_database_integrity.py)); **checkpoint
the validated private database back to R2, retried, before any public snapshot work**; only then
build and scan a sanitized public snapshot; upload it; and only on success, restart the public
Render service. A failure at any step fails the job and skips everything after it, so the last
good public snapshot and the last good private state are never replaced by a bad or partial one.

The private checkpoint's position -- immediately after validation, strictly before public snapshot
build/scan/publish -- is deliberate: the cycle's paid Stage A/B/C analyses exist only on the
runner's ephemeral disk until that checkpoint durably persists them to R2. Checkpointing before the
unrelated public-snapshot work means a failure in *that* work can never strand newly paid analyses
on a runner that is about to be discarded, which would otherwise cost a re-pay on the next run.

**R2 holds two distinct things.** A **private database** (`state/marketsentinel.db`, plus one
rolling `.bak`), read and written only by this workflow, downloaded at the start of a run and
checkpointed back right after validation. **Its download fails the job closed if the object is
missing** -- an empty-database start would silently discard the accumulated, already-paid-for
corpus, so R2's private bucket must be seeded once, manually, before the first scheduled run (see
README.md's Bootstrap steps); the workflow's own checkpoint keeps it current after that. The
constituent cache is exempt from this because it carries no paid state -- free, public data,
refetchable from Wikipedia -- so it alone keeps the soft fallback above. And a **public snapshot
bucket**, written as immutable, content-addressed objects (`snapshots/<version>/public-snapshot.db`
and `.../constituents_cache.json`, `version` a timestamp plus a hash of the built database) plus
one small mutable `latest.json` manifest carrying each asset's URL, sha256, and size. The versioned
objects upload first; `latest.json` uploads last, so a reader can never see it point at an object
that is not there yet.
[scripts/publish_public_snapshot.py](../../scripts/publish_public_snapshot.py) builds these by
calling into `scripts/build_deployment_snapshot.py`'s own `build_snapshot` / `describe_snapshot` /
`scan_artifacts` (now parameterized by path rather than hardcoded to `deploy/`), so a CI-published
snapshot goes through exactly the same `VACUUM INTO`, integrity check, and credential/
personal-data scan as the manually committed `deploy/` artifacts -- one implementation of that
bar, not two. It refuses to write a manifest (exit 1, no `latest.json`, no `version.txt`) if the
scan finds anything.

**The public deployment fetches its own snapshot at startup.**
[public_snapshot.py](../../src/marketsentinel/public_snapshot.py), run as
`python -m marketsentinel.public_snapshot` from the Docker image's `CMD` before uvicorn starts,
downloads `latest.json`, verifies both assets' sha256, runs SQLite's own `integrity_check` on the
downloaded database, and checks its `schema_user_version` against `SCHEMA_USER_VERSION`
(`storage/sqlite.py`) -- only then replacing `database_path` / `constituent_cache_path`, which the
image already populated at build time from the existing manually-committed
`deploy/public-snapshot.db`. `MARKETSENTINEL_PUBLIC_SNAPSHOT_MANIFEST_URL` is unset by default, so
this is a documented no-op and every existing standalone-image behaviour is unchanged. Any failure
-- unreachable manifest, hash mismatch, corrupt database, schema mismatch, or an unexpected
exception anywhere in the fetch -- is caught internally and leaves the baked-in snapshot untouched;
the function never raises, and the Dockerfile sequences the two commands with `;`, never `&&`, so
uvicorn starts regardless. A "publish" therefore ends in a **Render restart, not a redeploy**: the
container image never changes between coverage runs, only the snapshot it fetches on the way up.

## Shared public requests

[public_requests.py](../../src/marketsentinel/public_requests.py). The public deployment is a
read-only window and holds no LLM credential, yet any supported company must be able to *become*
covered and any stored, unanalysed article must be requestable -- by anyone, for everyone. The
shape is a **request drop-box**, not a queue service:

- **The public API records a request as one small JSON object** in a dedicated S3-compatible
  (Cloudflare R2) bucket -- `coverage/<TICKER>.json` or `articles/<TICKER>/<article_id>.json` --
  via `POST /api/v1/companies/{symbol}/coverage-requests` and
  `POST /api/v1/companies/{symbol}/articles/{article_id}/analysis-requests` (both `202`). Keys are
  idempotent, so a repeated request overwrites itself and the number of distinct requests is
  bounded by the universe plus the stored corpus. Nothing about the requester is stored. The
  bucket is the only durable state: the public host's disk is ephemeral (reset on every restart,
  and every publish restarts it), so the API keeps only an in-memory mirror of what is pending,
  rebuilt from the bucket at startup, and reports it through `/api/v1/capabilities`
  (`supports_coverage_requests`, `covered_companies`, `pending_coverage_requests`,
  `pending_article_requests`). The credential on the public host is scoped to this one bucket; it
  can never reach the private corpus or the published snapshot. Without a configured store the
  endpoints do not exist (`404`), like the spending endpoints in public mode.
- **Asking is capped; spending is capped elsewhere.** The API validates the ticker against the
  constituent universe and the article against the stored corpus (non-demo, no current-contract
  analysis), and refuses with `429` beyond the pending-coverage cap, the pending-article cap, the
  covered-plus-pending company ceiling, or the process-wide requests-per-minute limit (all
  `MARKETSENTINEL_PUBLIC_*` settings). None of these is what bounds spend.
- **The worker admits under hard per-run caps.** The scheduled workflow syncs the bucket into a
  directory, then `scripts/run_coverage_cycle.py --mode requests` activates at most
  `--max-new-tickers` requested companies (idempotent, spends nothing) and runs at most
  `--max-article-requests` article requests through the explicit-request ledger runner (the same
  `allow_terminal_retry` path as the private per-article endpoint, so a stored analysis is
  reused rather than re-paid). It writes the exact keys it consumed; after the private
  checkpoint the workflow deletes those keys and no others, so everything it did not reach --
  and everything that arrived during the run -- stays queued. The cycle then runs
  `--all-active` (every ticker in `company_coverage`), never-cycled first then least recently
  attempted, under `--max-tickers` (round-robin across runs) and `--max-new-total` (a hard
  ceiling on paid attempts in one invocation regardless of how many companies are covered).
- **Results reach everyone the existing way.** A processed coverage request is simply an active
  row in `company_coverage` plus stored articles and analyses in the next published snapshot; a
  processed article request is a stored analysis. No request table, no schema change, and the
  derived layers stay recomputed on read.

## Frozen fixtures vs the live database

Two different things, deliberately not reconciled:

- **Frozen fixtures** —
  [tests/fixtures/nvda_materiality_gold.json](../../tests/fixtures/nvda_materiality_gold.json), a
  hand-labelled materiality census, and
  [nvda_selector_gold.json](../../tests/fixtures/nvda_selector_gold.json), a selector and
  priority-signal regression set. Committed, self-contained, and scored with no database and no
  network: `uv run python scripts/evaluate_materiality.py evaluate --no-drift-check`. This is the
  reproducible form.
- **The live database** — `data/marketsentinel.db`, gitignored, and still collecting. Passing
  `--database` runs a drift check comparing every labelled row field by field against the stored
  analysis it was labelled from. It **fails once the corpus moves past the snapshot**, and that is
  the check working as designed, not a broken gate.

The evaluator always prints raw metrics; known-disagreement-adjusted figures appear beside them,
never instead of them. Its primary criterion is that no disagreement is unexplained. The figures
are in-sample regression pins on one company's labelled corpus, not out-of-sample validation.

The test suite is fully offline: fake providers, a static sentiment backend, and throwaway
databases under `data/test-runtime/` (see [tests/conftest.py](../../tests/conftest.py)).
