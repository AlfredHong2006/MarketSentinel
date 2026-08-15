# MarketSentinel

MarketSentinel is a financial-news sentiment and market-direction research app. It combines recent
RSS headlines, FinBERT probabilities, stored daily sentiment, historical market data, and an
experimental five-trading-day direction baseline behind a FastAPI API and Streamlit dashboard.

> **Important:** MarketSentinel is an educational portfolio project, not financial advice.
> Sentiment does not itself predict prices, and the forecast is not an exact price prediction or a
> calibrated guarantee of an investment outcome.

## What phase 1 does

- Searches the current S&P 500 and FTSE 100 constituent tables, with a 24-hour local cache.
- Fetches three years of adjusted daily price/volume data with `yfinance` and displays the latest 30
  trading sessions.
- Fetches **recent** Google News RSS items through a replaceable provider interface.
- Normalizes titles and URLs, applies transparent relevance rules, and deduplicates syndicated
  headlines using a stable fingerprint.
- Lazily loads `ProsusAI/finbert`, scores new headlines in batches, and maps logits through
  `model.config.id2label` rather than assuming label order.
- Stores articles, FinBERT results, and daily aggregates in SQLite.
- Shows the genuine sentiment dates accumulated so far and a seven-calendar-day moving average.
- Produces an experimental probability that the adjusted close will be higher in five trading
  sessions, with chronological validation and visible naive baselines.
- Reports source health. An HTTP 200 with no valid records is degraded, not healthy.
- Can use clearly labelled synthetic demo headlines if live RSS is unavailable; tests never require
  network access or the real model.

## Data honesty

RSS is a recent-news feed, not a reliable historical-news archive. MarketSentinel therefore does
**not** manufacture a 30-day sentiment series from RSS:

- The price chart displays 30 trading sessions.
- The sentiment chart displays only dates backed by available scored articles.
- SQLite retains new observations, so this chart becomes richer through actual use.
- Missing sentiment dates are not backfilled with invented values.
- The forecast begins as a price/volume baseline. Sentiment features activate only after at least 10
  stored sentiment dates are present, and the UI reports the exact coverage.

A later phase should integrate a properly licensed historical-news source and use walk-forward
experiments to measure whether sentiment improves out-of-sample performance over price-only
baselines.

## Architecture

```mermaid
flowchart LR
    U["Current index constituents"] --> API["FastAPI service"]
    RSS["Google News RSS"] --> V["Validate + source health"]
    DEMO["Marked demo fallback"] --> V
    V --> N["Normalize + relevance + deduplicate"]
    N --> DB[("SQLite")]
    N --> F["Lazy batched FinBERT"]
    F --> DB
    DB --> A["Daily aggregation + 7-day MA"]
    YF["yfinance adjusted bars"] --> M["Leakage-safe logistic baseline"]
    A --> M
    A --> API
    M --> API
    API --> UI["Streamlit + Plotly dashboard"]
```

The package follows a `src/` layout with explicit boundaries:

```text
src/marketsentinel/
├── aggregation/       # Daily sentiment and moving averages
├── api/               # FastAPI boundary and dependency wiring
├── forecasting/       # Chronological five-session baseline
├── sentiment/         # Lazy FinBERT service
├── sources/           # RSS and market-data adapters
├── storage/           # SQLite repository
├── constituents.py    # Current universe discovery/cache
├── dashboard.py       # Streamlit client
├── domain.py          # Pydantic domain contracts
├── normalization.py   # Relevance and deduplication
└── service.py         # End-to-end orchestration
```

## Scoring and aggregation

FinBERT returns positive, negative, and neutral probabilities. For headline `i`, MarketSentinel
defines a signed sentiment index:

```text
s_i = P(positive_i) - P(negative_i), where -1 <= s_i <= 1
```

This is a derived index, not a probability of price movement. The code reads the probability indices
from the model configuration. For `ProsusAI/finbert`, the published mapping is positive at index 0,
negative at index 1, and neutral at index 2; a regression test protects this historically error-prone
boundary.

For an article age `a_i` in hours and configurable half-life `h` (24 hours by default):

```text
w_i = 2 ^ (-a_i / h)
daily_sentiment = sum(w_i * s_i) / sum(w_i)
```

The seven-day line is the arithmetic mean of available daily aggregates in the current date and the
previous six calendar days. Dates with no articles are absent rather than silently imputed as neutral.

## Experimental forecast

The target at trading session `t` is:

```text
y_t = 1 if adjusted_close_(t+5) > adjusted_close_t, otherwise 0
```

Features use only information available at `t`:

- One- and five-session trailing returns
- Ratios to five- and 20-session moving averages
- Ten-session annualized volatility
- Five-session relative volume
- Daily sentiment, seven-day sentiment average, and article count when coverage is sufficient

The model is a regularized logistic regression with median imputation and standardization. The newest
20% of labelled observations (at least 30) form a chronological validation window. A five-session
purge separates training from validation so no training label reaches into validation dates; there is
no random time-series split. Daily sentiment is conservatively assigned to the next observed trading
session, preventing an after-close headline from becoming a same-close feature. The dashboard compares
accuracy with a training-majority classifier and a simple trailing-momentum rule, then refits on all
matured labels for the current estimate.

The output probability is **not calibrated**. A future evaluation should use walk-forward splits,
probability calibration, multiple market regimes, transaction-cost-aware decision rules, and a
licensed historical sentiment dataset.

## Run locally

Requirements: [uv](https://docs.astral.sh/uv/) and Python 3.11. Docker is not required for phase 1.

```powershell
git clone <your-repository-url>
cd MarketSentinel
uv sync --dev
Copy-Item .env.example .env
```

Start the API:

```powershell
uv run uvicorn marketsentinel.api.app:app --reload
```

Then start the dashboard in a second terminal:

```powershell
uv run streamlit run src/marketsentinel/dashboard.py
```

Open `http://localhost:8501`. API documentation is available at `http://127.0.0.1:8000/docs`.

The first analysis that has unscored articles downloads the public `ProsusAI/finbert` model and may
take longer. The model is loaded once per API process. No Hugging Face token is required for this
public model.

## Configuration

Settings use the `MARKETSENTINEL_` prefix and may be placed in `.env`. See `.env.example` for the full
starter configuration.

| Setting | Default | Purpose |
| --- | --- | --- |
| `DATABASE_PATH` | `data/marketsentinel.db` | Local SQLite runtime database |
| `FINBERT_MODEL` | `ProsusAI/finbert` | Hugging Face model identifier |
| `FINBERT_DEVICE` | `auto` | CPU/CUDA selection; CPU is the default fallback |
| `FINBERT_BATCH_SIZE` | `8` | Inference batch size |
| `NEWS_LOOKBACK_DAYS` | `7` | RSS recency window |
| `NEWS_MAX_ARTICLES` | `50` | Maximum articles per analysis |
| `ALLOW_DEMO_FALLBACK` | `true` | Permit visibly labelled synthetic fallback headlines |

The `.env`, SQLite databases, model caches, virtual environments, and provider caches are ignored by
Git. API credentials can be added to the settings/provider boundary later; they must never be
committed.

## API

```text
GET  /health
GET  /api/v1/constituents/search?q=Apple&market=S%26P%20500&limit=20
POST /api/v1/analyze
```

Example request:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/analyze `
  -ContentType application/json `
  -Body '{"symbol":"AAPL"}'
```

## Quality checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=marketsentinel --cov-report=term-missing
```

Tests inject fake providers and a static sentiment backend, so CI is deterministic and does not fetch
live data or download model weights. The suite covers FinBERT label mapping, normalization,
deduplication, recency weighting, calendar-window aggregation, SQLite round-trips, sparse-sentiment
gating, chronological forecast targets, and the complete service orchestration path.

## Known limitations

- Google News RSS offers recent discovery, not dependable historical coverage or a guaranteed API.
- The relevance stage is transparent and testable but still title-based and imperfect, especially for
  short or ambiguous tickers.
- Google News links may be redirect URLs, and different publishers can describe the same event with
  different headlines.
- The constituent list depends on validated Wikipedia table structure. A cached or small offline
  fallback is clearly identified if refresh fails.
- `yfinance` is suitable for a portfolio demonstration, not a contractual production market-data
  feed.
- Sentiment probabilities describe text tone. They are not calibrated probabilities of price moves.
- Forecast validation is a baseline diagnostic, not evidence of a tradable strategy. Backtests do not
  guarantee future performance.
- Demo headlines are synthetic, always marked, and never presented as real news.

## Next engineering steps

1. Add a licensed historical-news provider behind the existing adapter interface.
2. Compare price-only and price-plus-sentiment models with walk-forward evaluation and confidence
   intervals.
3. Add a small opt-in real-model integration test and a labelled financial-language evaluation.
4. Add scheduled ingestion so SQLite collects daily observations without manual searches.
5. Add Docker packaging and deployment configuration after the local product flow is stable.
6. Capture dashboard screenshots and publish a short reproducible demo.
