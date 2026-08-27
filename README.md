# MarketSentinel

[![CI](https://github.com/AlfredHong2006/MarketSentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/AlfredHong2006/MarketSentinel/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)

**Evidence-grounded company intelligence from financial news, corporate events, and market data.**

Point MarketSentinel at any S&P 500 or FTSE 100 company and it answers the question a tone score
cannot: *what actually happened to this business, and how well is it evidenced?* Hundreds of
headlines become a short, auditable list of **material developments** — each a typed corporate
event, merged across syndicated reporting, passed through a four-condition materiality gate, and
annotated with the publishers that corroborated it.

**125-row hand-labelled evaluation** · **raw gate P 0.907 / R 0.942** · **0 unexplained
disagreements** (in-sample) · **711 deterministic tests** · **evaluation runs offline from a fresh
clone**

![MarketSentinel market overview for NVDA: price, news sentiment, and analysed-event markers on one chart, above a plain-language Current Market View](docs/images/market-overview.png)

*One company, one screen, with toggleable layers and timeframes. Sentiment points are **observed
only** — dates with no coverage are absent, never interpolated to neutral — and the Current Market
View reports each finding separately rather than as one blended score.*

---

## What it does

| Surface | What you get |
| --- | --- |
| **Current Market View** | Price move, recent observed sentiment, the highest evidenced downside concern, and the latest material event — four separate statements, not a verdict. |
| **Key Developments** | The strongest material developments, ranked. One row is one *underlying event*, not one article. |
| **Today's Intelligence** | The evidence behind each event: corroborating publishers, transmission channels, related companies, full stored analysis. |
| **Top Risks** | Downside themes ranked by a bounded 0–100 **Concern Index**, each backed by named signals, publisher counts, and a dated link. |

> **Not financial advice.** An educational portfolio project. Nothing here is a price prediction,
> a calibrated probability of an investment outcome, or a recommendation.

---

## Why this is not just sentiment analysis

Most "AI market sentiment" projects stop at *headline → FinBERT → average tone → line chart*.
That pipeline has three well-known failure modes, and MarketSentinel is built around fixing them.

| Failure mode of tone-only dashboards | What MarketSentinel does instead |
| --- | --- |
| **Tone is not an event.** "Nvidia shares slip 3%" and "Nvidia loses a $10bn contract" can score identically, yet only one changes the business. | Extracts a **typed event** — acquisition, regulation, litigation, supply disruption — with direction, magnitude, and a persistence horizon. Market reactions cannot stand in as the underlying event. |
| **Volume is mistaken for signal.** One story syndicated by 20 outlets looks like 20 confirmations, inflating any average. | **Groups** duplicate reporting into a single row, and scores each risk theme by its **strongest signal plus a capped corroboration bonus — never an open-ended sum**. |
| **Nothing is falsifiable.** A blended 0–100 "sentiment score" cannot be argued with. | Every verdict is a set of **auditable booleans**, and a rejected article names the condition that failed. The gate is scored against a hand-labelled gold corpus with published precision and recall. |

Two rules run through the codebase. **Corroboration is conservative:** only the articles a claim
actually *cited* count, deduplicated to distinct publishers. They are called **external** sources,
not *independent* ones — nothing in the stored data establishes editorial independence. **Nothing
is imputed:** missing sentiment dates stay absent, and an HTTP 200 carrying no valid records is
reported as *degraded*.

---

## Pipeline

| Stage | What happens | Implementation |
| --- | --- | --- |
| **1. Ingest** | GDELT DOC 2.0 backfill plus recent Google News RSS, with a date-bounded fallback only when GDELT accepts nothing. Deduplication uses provider IDs, canonical URLs, or identical normalized title + publisher within six hours — never fuzzy matching. | [sources/historical.py](src/marketsentinel/sources/historical.py), [normalization.py](src/marketsentinel/normalization.py) |
| **2. Event extraction** (Stage A) | A typed LLM call extracts one business event per article: type, direction, magnitude, horizon, and transmission channels. Article text is **untrusted data** — embedded instructions are never followed, and predictions never become established facts. | [event_analysis.py](src/marketsentinel/event_analysis.py) (`event-extraction-v7`) |
| **3. Evidence** (Stage B) | Claims are assessed **only** against supplied stored articles from a temporally bounded window; pretrained knowledge is not evidence. Each returns corroborated / contradicted / unsupported / uncertain plus the article IDs it cited. | [event_analysis.py](src/marketsentinel/event_analysis.py) (`claim-evidence-v1`) |
| **4. Materiality gate** | Four deterministic conditions in order — **guard** (not commentary or a price move), **driver** (names a real cash-flow or risk driver), **durability** (persists beyond the news cycle), **evidence** (qualifying source class, external support, or first-hand disclosure). The first failure is recorded by name; a *contradicted* claim never rejects a row, the dispute is attached instead. | [materiality.py](src/marketsentinel/materiality.py) |
| **5–6. Grouping and ranking** | Material events merge by transitive same-development similarity (shared anchor terms and title overlap), so several reports of one event become one row represented by its strongest member. Groups are then ordered **lexicographically, never blended**: magnitude leads, event-class tier breaks ties, evidence breadth breaks the rest. | [materiality.py](src/marketsentinel/materiality.py) (`group_material_events`, `prepare_key_developments`) |
| **7. Risks** | Stored events map through a fixed downside taxonomy — no embeddings, no fuzzy matching, no LLM classifier — into a bounded 0–100 Concern Index per theme from severity, evidence support, and persistence-aware recency decay. Unrecognised mechanisms become `UNMAPPED`. | [risk_taxonomy.py](src/marketsentinel/risk_taxonomy.py), [risk_signals.py](src/marketsentinel/risk_signals.py), [risk_scoring.py](src/marketsentinel/risk_scoring.py) |

**Nothing in stages 4–7 is persisted.** Verdicts, groups, and rankings recompute from stored
analyses on every request, so a policy change needs no migration and no re-analysis.

A third LLM stage (`related-company-v5`) proposes related-company effects, but only from a supplied
candidate list and only where a concrete, event-specific transmission mechanism exists — shared
sector membership is insufficient.

---

## Key Developments

![Key Developments for NVDA: ranked development cards with Impact, Direction, Persistence, and Corroboration, above a funnel caption](docs/images/key-developments.png)

Each card is one underlying development, with **Impact**, **Direction**, **Persistence**, and
**Corroboration** stated separately rather than fused into one number. A group of several reports
expands in place (`All 3 reports · 3 publishers`). Two details worth noting above:

- The funnel caption (`131 analysed → 58 material → 53 developments · showing the strongest 8`)
  states what was examined, what passed the gate, and how many rows grouping merged away. These are
  **live-database** figures from the moment of capture, deliberately larger than the frozen 125-row
  evaluation snapshot below — the corpus kept collecting after the gold labels were frozen.
- Corroboration reads `None found` where a claim has no external support. A well-evidenced row and
  a single-publisher row look different.

---

## Today's Intelligence

![Today's Intelligence for NVDA: per-event evidence cards showing corroboration, transmission channels, and related companies](docs/images/todays-intelligence.png)

The audit trail. Each event states its corroboration in plain words — `1 claim corroborated ·
1 external source`, or `Supported only by the same publisher` — plus transmission channels, which
read `None identified from supplied evidence.` when the model found none. Related companies carry a
stated mechanism per name, not a sector label. **View analysis** expands the full stored record and
**Original article** links out, so any row can be checked against its source. Ordering is stated in
the UI: magnitude, extraction confidence, evidence strength, source class, publication time.

---

## Top Risks

![Top Risks for NVDA: downside themes ranked by Concern Index with supporting signal counts and dated sources](docs/images/top-risks.png)

Stored events map through a **fixed taxonomy** into ranked downside themes. Each row carries the
mechanism in one sentence, the number of supporting signals, how many publishers they span, the
latest date, and a source link.

> The **Concern Index** is an evidence-weighted salience ranking. It is not a probability, an
> expected loss, or a price prediction, and it is not comparable across companies. The UI says so
> too.

---

## Evaluation

The materiality gate is scored against a **frozen, hand-labelled gold corpus** committed at
[tests/fixtures/nvda_materiality_gold.json](tests/fixtures/nvda_materiality_gold.json) — a labelled
*snapshot*, not a live view of the database. The live corpus grows; the snapshot does not.

**These are in-sample regression figures, not out-of-sample validation.** The gate's thresholds and
vocabulary were developed against this same corpus, so they pin behaviour against regression but do
**not** show that the gate generalises to other tickers, sectors, or regimes.

| Corpus (NVDA, frozen 2026-08-26) | | Gate performance (raw) | |
| --- | --- | --- | --- |
| Labelled stored analyses | **125** | Gate precision | **0.907** |
| Gold-labelled material | **52** | Gate recall | **0.942** |
| Gate calls material | **54** | Grouping precision (pairwise) | **1.000** |
| Distinct developments after grouping | **49** | Grouping recall (pairwise) | **0.778** |
| | | **Unexplained disagreements** | **0** |

Raw confusion: `tp 49 | fp 5 | fn 3 | tn 68`. Grouping is scored pairwise over the rows gold and the
gate both call material: 9 gold pairs, 7 predicted.

### Against naive baselines on the same labels

| Selector | Precision | Recall |
| --- | --- | --- |
| **Materiality gate** | **0.907** | **0.942** |
| Meaningful-event floor | 0.526 | 0.981 |
| Top-N by magnitude, then confidence | 0.630 | 0.654 |

The meaningful-event floor selects nearly twice as many rows as the gate (97 against 54) and
produces roughly nine times the false positives (46 against 5); ranking by magnitude alone loses
both precision and recall. The gate's *structure* — not merely having an LLM in the loop — produces
the separation.

**The pass criterion is explanation, not a threshold.** Every false positive, false negative, and
missed grouping pair must be named in `known_disagreements` with a reason; `unexplained
disagreements: 0` is the result that matters, and the numeric tripwires are secondary.[^adjusted]

[^adjusted]: Excluding the 10 disagreements documented with named reasons in the fixture, all four
figures reach 1.000. That is an argument about which errors were knowingly accepted — not evidence
they did not happen — so the raw figures stay the headline.

---

## Architecture and engineering

```mermaid
flowchart TB
    subgraph Ingest["Ingestion"]
        GDELT["GDELT + RSS + marked demo fallback"] --> V["Validate + source health"]
        V --> N["Normalize + relevance + deduplicate"]
    end

    N --> DB[("SQLite: articles, scores, analyses")]
    N --> F["Lazy batched FinBERT"] --> DB

    subgraph Intel["Event intelligence (cached, typed LLM stages)"]
        SEL["Deterministic candidate selection"] --> A["A: event extraction"]
        A --> B["B: claim evidence"] --> C["C: related companies"]
    end

    DB --> SEL
    C --> DB

    subgraph Derive["Deterministic, recomputed per request"]
        MAT["Materiality gate"] --> GRP["Grouping"] --> KD["Ranked developments"]
        RSK["Risk taxonomy to Concern Index"]
    end

    DB --> MAT
    DB --> RSK
    DB --> AGG["Daily aggregation + trend"]

    KD --> API["FastAPI service"]
    RSK --> API
    AGG --> API
    API --> UI["Streamlit + Plotly dashboard"]
    API --> DIAG["Research diagnostics"]
```

A `src/` layout with explicit boundaries (abridged — key modules only):

```text
src/marketsentinel/
├── aggregation/            # Daily sentiment and moving averages
├── api/                    # FastAPI boundary and dependency wiring
├── forecasting/            # Chronological five-session baseline (diagnostic)
├── sentiment/              # Lazy FinBERT service
├── sources/                # GDELT, RSS, and market-data adapters
├── storage/                # SQLite repository
├── analysis_candidates.py  # Deterministic article selection
├── event_analysis.py       # Three-stage typed LLM intelligence
├── materiality.py          # Gate, grouping, ranking
├── risk_taxonomy.py        # Event to downside-theme mapping
├── risk_signals.py         # Risk-signal extraction
├── risk_scoring.py         # Bounded Concern Index aggregation
├── dashboard*.py           # Streamlit client and pure view models
├── domain.py               # Pydantic domain contracts
└── service.py              # End-to-end orchestration
```

- **Deterministic where it matters.** Stages 4–7 contain no LLM call, no embedding, and no fuzzy
  match, so the same stored analyses always produce the same developments, groups, and ranking.
- **View preparation is pure.** The `dashboard_*.py` modules take payloads and return view models,
  making every rendered surface unit-testable without Streamlit.
- **LLM output is a typed contract.** All three stages use structured outputs validated against
  Pydantic models, and the cache key includes prompt and schema version, so a version bump
  invalidates cleanly.
- **Failure is visible.** Source health distinguishes healthy, degraded, and failed; demo fallbacks
  are labelled synthetic, never presented as real news.

### Sentiment index

FinBERT returns positive/negative/neutral probabilities. For headline `i`, age `a_i` in hours,
relevance `r_i`, half-life `h` (24h default), and probability entropy `H(p_i)`:

```text
s_i          = P(positive_i) - P(negative_i),  -1 <= s_i <= 1
confidence_i = 1 - H(p_i) / log(3)
lambda       = log(2) / h
w_i          = r_i * confidence_i * exp(-lambda * a_i)
S_t          = sum(w_i * s_i) / sum(w_i)
```

Probability indices are read from `model.config.id2label` rather than assuming label order, with a
regression test pinning that historically error-prone boundary. `S_t` is a derived index over
observed dates, not a probability of price movement.

### Baseline forecast (research diagnostic)

A five-trading-day direction baseline sits inside the collapsed **Research diagnostics** expander.
It is not a headline feature: it exists to demonstrate leakage-safe time-series handling, not to
predict prices.

The target at session `t` is `y_t = 1 if adjusted_close_(t+5) > adjusted_close_t else 0`, and
features use only information available at `t`. A regularized logistic regression is validated
chronologically on the **newest 20% (minimum 30 sessions)** of labelled observations with a
**five-session purge** between training and validation, and daily sentiment is assigned to the
*next* observed trading session so an after-close headline cannot become a same-close feature.
Training-majority and trailing-momentum baselines sit beside it.

**The output probability is not calibrated.** Proper evaluation would need walk-forward splits,
calibration, multiple regimes, cost-aware decision rules, and a licensed historical sentiment
dataset. See [forecasting/baseline.py](src/marketsentinel/forecasting/baseline.py).

---

## Reproducibility

The gold fixture is committed, so **the evaluation runs from a fresh clone with no database and no
network access**:

```bash
uv run python scripts/evaluate_materiality.py evaluate --no-drift-check
```

That prints the raw confusion matrix, both baselines, every documented disagreement with its
reason, the threshold checks, and — in the tool's own output — the reminder that these are
in-sample regression figures.

**The live-DB drift check is optional.** Adding a database compares every labelled row field by
field against the stored analysis it was labelled from:

```bash
uv run python scripts/evaluate_materiality.py evaluate --database data/marketsentinel.db
```

This reports `FAIL` once the database has collected or re-analysed articles the frozen fixture does
not cover. **That is the drift check working as designed, not a broken gate** — a gold set that
silently disagrees with a re-analysed corpus measures nothing. The `--no-drift-check` form above is
the reproducible one.

Tests inject fake providers and a static sentiment backend, so the suite is **deterministic and
never touches the network or downloads model weights**:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=marketsentinel --cov-report=term-missing
```

**711 tests pass** with lint and formatting clean; CI runs all three on every push and pull request
against a locked dependency set. Coverage spans FinBERT label mapping, normalization and
deduplication, SQLite round-trips, chronological forecast targets, the three-stage analysis
contracts, risk scoring, the materiality gate and grouping, the gold-census harness, and the full
service orchestration path.

---

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.11.

```bash
git clone https://github.com/AlfredHong2006/MarketSentinel.git
cd MarketSentinel
uv sync --dev
cp .env.example .env          # Windows: Copy-Item .env.example .env
```

Then start the API and dashboard — on Windows, one script runs both:

```powershell
.\scripts\run_local.ps1
```

On macOS or Linux, run the two processes directly:

```bash
uv run uvicorn marketsentinel.api.app:app --host 127.0.0.1 --port 8000 &
uv run streamlit run src/marketsentinel/dashboard.py --server.port=8501
```

Dashboard at `http://localhost:8501`; API docs at `http://127.0.0.1:8000/docs`.

The first analysis with unscored articles downloads the public `ProsusAI/finbert` model and takes
longer; it loads once per API process and needs no Hugging Face token. **Event intelligence
(stages A–C) is optional** — without `MARKETSENTINEL_LLM_API_KEY` the app runs normally and the
intelligence surfaces stay empty rather than failing.

### Configuration

Settings use the `MARKETSENTINEL_` prefix and may be placed in `.env`. The ones worth knowing:

| Setting | Default | Purpose |
| --- | --- | --- |
| `LLM_API_KEY` | unset | Enables the three typed article-intelligence stages |
| `LLM_MODEL` | `gpt-4o-mini` | Model used by all three stages |
| `ANALYSIS_AUTO_MAX_NEW_PER_RUN` | `6` | Uncached analysis attempts per run; `0` is a kill switch |
| `DATABASE_PATH` | `data/marketsentinel.db` | Local SQLite runtime database |
| `FINBERT_DEVICE` | `auto` | CPU/CUDA selection; CPU is the fallback |
| `ALLOW_DEMO_FALLBACK` | `true` | Permit visibly labelled synthetic fallback headlines |

See [.env.example](.env.example) for the full configuration, including FinBERT batch size, news
lookback windows, and GDELT request pacing. `.env`, SQLite databases, and caches are gitignored.

### API

```text
GET  /health
GET  /api/v1/constituents/search?q=Apple&market=S%26P%20500&limit=20
POST /api/v1/analyze              # full company analysis
POST /api/v1/articles/analyze     # manual per-article intelligence
```

### Stack

**Python 3.11** · **FastAPI** + **Uvicorn** · **Streamlit** + **Plotly** · **Pydantic v2** ·
**SQLite** · **transformers** + **PyTorch** (FinBERT) · **scikit-learn** · **pandas** / **NumPy** ·
**OpenAI** typed structured outputs · **httpx** / **requests** / **feedparser** · **tenacity** ·
**uv** (locked dependencies) · **ruff** · **pytest** + pytest-cov · **GitHub Actions**.

---

## Limitations

- **The evaluation is in-sample and single-ticker** — 125 labelled NVDA analyses. It pins
  regression; it is not evidence of generalisation.
- **Extraction carries no issuer/subject distinction**, so a third party's financing to buy the
  subject's products can read as the subject's own event. Four of the five documented false
  positives are this one missing field.
- **Relevance and grouping are title-based**: transparent and testable, but imperfect for short or
  ambiguous tickers. Two publishers can describe one event with titles sharing no anchor terms —
  the two documented grouping misses are exactly this.
- **GDELT and Google News RSS are free discovery sources**, not complete or licensed archives, and
  GDELT can rate-limit or return malformed responses.
- **Stage A/B/C outputs are LLM-generated** — validated structurally and semantically, but not
  guaranteed correct.
- Sentiment probabilities describe **text tone**, not probabilities of price moves.
- The constituent list depends on validated Wikipedia table structure with a cached fallback;
  `yfinance` suits a demonstration, not a production market-data feed.

## Roadmap

1. Label a second ticker in a different sector and report the gate's **out-of-sample** precision
   and recall — the single most valuable next result.
2. Carry an issuer/subject distinction through extraction, closing four of the five documented
   false positives.
3. Add a licensed historical-news provider behind the existing adapter interface.
4. Scheduled ingestion so SQLite accumulates observations without manual searches.
5. Docker packaging and a deployed demo.

## License

[MIT](LICENSE) © 2026 Alfred Hong
