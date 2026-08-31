# CLAUDE.md

Entrypoint for Claude sessions in this repository. Read this first, then follow the links below
for depth. Working rules are in [AGENTS.md](AGENTS.md) and are binding.

## What MarketSentinel is

An evidence-grounded **company-intelligence** system for medium- and long-term investors. It
compresses high-volume financial news about one public company into a small number of material,
evidence-backed developments an investor can inspect and argue with.

It is not a sentiment dashboard, news reader, trading bot, or price-prediction product. Sentiment,
price charts, and the direction forecast exist as supporting context only.

## The killer loop

```
financial news → structured company events → claim/evidence checking → materiality assessment
→ same-development grouping → ranked Key Developments → persistent Top Risks
```

The goal is the *smallest useful set* of genuinely material developments, not maximum event count.

## Repository layout

```
src/marketsentinel/
  service.py                  orchestration of one end-to-end company analysis
  domain.py                   all Pydantic contracts, enums, and projections
  config.py                   MARKETSENTINEL_-prefixed settings
  api/app.py                  FastAPI boundary + dependency wiring (build_services)
  dashboard.py                Streamlit client (talks to the API over HTTP)

  sources/                    news.py (Google News RSS + demo), historical.py (GDELT + RSS
                              fallback), prices.py (yfinance)
  normalization.py            fingerprints, relevance, deterministic deduplication
  storage/sqlite.py           the only persistence layer; schema + versioned analysis cache

  analysis_candidates.py      deterministic selection of which articles get a paid analysis
  event_analysis.py           Stage A/B/C provider, context building, validation, caching
  analysis_compatibility.py   the exact-equality version contract for stored analyses
  event_policy.py             the single shared meaningful-event floor
  subject_principal.py        the shared rule for whether the subject company is a principal

  materiality.py              materiality gate, grouping, Key Developments ranking
  overview.py                 typed Company Overview projection; owns no rule of its own
  risk_signals.py / risk_taxonomy.py / risk_scoring.py    persistent Top Risks
  dashboard_intelligence.py   corroboration semantics + Today's Intelligence
  dashboard_*.py              pure presentation preparation per dashboard section

  backfill_service.py / historical_backfill.py    manual one-shot historical backfill
  aggregation/, sentiment/, forecasting/          supporting infrastructure
scripts/                      manual CLIs: backfill, materiality evaluation, audit, smoke
tests/fixtures/               frozen labelled gold sets
```

Full pipeline and module boundaries: [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md).

## Core architectural invariants

1. **Extraction and materiality stay separate.** LLMs may extract *what an article says*
   (`event_analysis.py`). Whether it *deserves investor attention* is a deterministic, auditable
   layer (`materiality.py`). Do not move materiality into a prompt.
2. **Only Stage A/B/C analyses are persisted.** Materiality verdicts, groups, rankings, and risk
   scores are recomputed from stored analyses on every request, so a policy change needs no
   migration and no re-analysis.
3. **Stored analyses are versioned and immutable.** A cache row is keyed by article fingerprint +
   model version + cache version + schema version, and never overwritten.
   `ArticleAnalysisCompatibility` applies **exact equality** on all version fields before an
   analysis may be displayed or reused.
4. **Article text is untrusted data.** It is fenced in the prompt, never followed as instruction,
   and provider output is validated structurally (Pydantic) *and* semantically (claim IDs, cited
   evidence IDs, and related tickers must come from what the application supplied).
5. **Evidence semantics are honest.** Issuer-controlled channels never count as external
   corroboration of the issuer's own claim. Use "external source", not "independent source".
   Contradictions stay visible; a contradicted claim never silently deletes a development.
6. **Duplicate reporting adds breadth, not importance.** Grouping merges reports of one event;
   risk aggregation takes the strongest signal plus a small bounded bonus, never a sum.
7. **Failure is safe.** An analysis that cannot be produced returns a typed status
   (`unavailable` / `failed` / `not_found`), never a fabricated result.
8. **Determinism.** Selection, materiality, grouping, ranking, and risk scoring take no clock, no
   I/O, and no randomness — `now` is injected.
9. **Frozen fixture ≠ live DB.** `tests/fixtures/*.json` are frozen research snapshots. The live
   database keeps growing. A drift check that fails because the corpus moved is expected.

## Standard commands

```bash
uv sync --dev                                            # install (Python 3.11, locked)
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=marketsentinel --cov-report=term-missing   # offline and deterministic
uv run python scripts/evaluate_materiality.py evaluate --no-drift-check   # reproducible gate score
```

Run locally (Windows): `.\scripts\run_local.ps1` — starts the API on 8000 and the dashboard on 8501.
Elsewhere: `uv run uvicorn marketsentinel.api.app:app --host 127.0.0.1 --port 8000` plus
`uv run streamlit run src/marketsentinel/dashboard.py --server.port=8501`.

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs lint, format check, and tests on
every push and pull request.

Relevant validation is required for any change; a materiality, grouping, or selection change also
needs the evaluation command above.

## What must not casually change

- **Prompt/schema version constants** in [src/marketsentinel/event_analysis.py](src/marketsentinel/event_analysis.py)
  (`STAGE_A_PROMPT_VERSION`, `STAGE_B_PROMPT_VERSION`, `STAGE_C_PROMPT_VERSION`,
  `ARTICLE_ANALYSIS_SCHEMA_VERSION`). Bumping one invalidates every stored analysis for display
  and cache reuse, and re-earning them costs real LLM spend.
- **The exact-equality compatibility rule** in `analysis_compatibility.py`.
- **The shared meaningful-event floor** in `event_policy.py`. It is read by the materiality gate,
  Today's Intelligence, chart markers, Stage C eligibility, and risk signal extraction; moving it
  moves all of them at once.
- **Materiality conditions, thresholds, and vocabulary** in `materiality.py`, and the grouping
  windows/overlaps there and in `risk_scoring.py`.
- **The subject-principal rule** in `subject_principal.py`. It is read by the materiality gate and
  by risk signal extraction, and every rule in it deletes a development, so it recognises one
  narrow shape deliberately; loosening it is a product decision, not a fix.
- **Risk bonus caps and the severe-band gate** in `risk_scoring.py`.
- **The evidence window** (`EVIDENCE_WINDOW_DAYS_BEFORE/AFTER`) and evidence ranking, which feed
  the cache-invalidating `evidence_fingerprint`.
- **The SQLite schema and `PRAGMA user_version`** in `storage/sqlite.py`.
- **Frozen gold fixtures** in `tests/fixtures/`. Relabelling to improve a metric is not a fix.
- Anything that would turn the forecast or Concern Index into a stated trading signal.

## Document map

- [docs/product/PRODUCT.md](docs/product/PRODUCT.md) — what the product is for, principles,
  priority test, what is explicitly *not* the USP.
- [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) — the real pipeline,
  module boundaries, cache/version/evidence semantics.
- [docs/decisions/DECISIONS.md](docs/decisions/DECISIONS.md) — settled decisions that survive
  individual chats. Treat as binding.
- [AGENTS.md](AGENTS.md) — how AI agents must work in this repository.
- [README.md](README.md) — public-facing presentation, screenshots, evaluation figures.

Product direction, scope, priorities, and subjective UX are the product owner's decisions.
