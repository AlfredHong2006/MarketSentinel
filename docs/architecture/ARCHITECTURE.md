# MarketSentinel Architecture

Derived from the current codebase. Describes what exists, not what might be built.

Product intent lives in [PRODUCT.md](../product/PRODUCT.md); settled decisions in
[DECISIONS.md](../decisions/DECISIONS.md); the session entrypoint is [CLAUDE.md](../../CLAUDE.md).

## Shape of the system

Two processes over one SQLite file:

- **API** — FastAPI ([api/app.py](../../src/marketsentinel/api/app.py)). `build_services` wires
  every concrete provider once; `create_app` exposes `/health`, `/api/v1/constituents/search`,
  `/api/v1/analyze`, `/api/v1/companies/{symbol}/overview`, and `/api/v1/articles/analyze`.
  Allowed browser origins come from `cors_allow_origins` in
  [config.py](../../src/marketsentinel/config.py) rather than being hardcoded.
- **Dashboard** — Streamlit ([dashboard.py](../../src/marketsentinel/dashboard.py)). A pure HTTP
  client. It holds no database handle and calls no provider; every section it renders is prepared
  by a `dashboard_*` module from the JSON payload.

Domain contracts are Pydantic models in [domain.py](../../src/marketsentinel/domain.py), which is
also where the two projections live: `ArticleAnalysis.to_analyzed_event()` (chart markers) and
`.to_company_intelligence_event()` (the product-facing record every downstream layer reads).

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

### 4. Persistence and versioning

[storage/sqlite.py](../../src/marketsentinel/storage/sqlite.py) is the only persistence layer.
Tables: `articles`, `sentiments`, `daily_sentiment`, `article_intelligence_analyses`
(`PRAGMA user_version = 4`, WAL, additive column migrations applied at `initialize()`).

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
cache only, so opening the product costs neither ingestion nor LLM spend. Price history is the one
exception, because it is not persisted: it is fetched live, and a failed fetch reports an
unavailable chart rather than substituting a series. Refreshing coverage stays an explicit
`POST /api/v1/analyze`.

### 8. Presentation

`dashboard_*` modules are pure and read the JSON payload only:

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
`dashboard_charts.py`, and the FastAPI and Streamlit boundaries themselves.

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
