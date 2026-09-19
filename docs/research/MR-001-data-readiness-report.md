# MR-001 — Historical Market Reaction data readiness

Inspected: 2026-09-19. Database: `data/marketsentinel.db` (last written 2026-09-13, schema
`user_version = 5`), opened read-only. Prices: one yfinance snapshot taken 2026-09-19 (3y daily).
Methodology under test: [HISTORICAL_MARKET_REACTION_V1.md](../product/HISTORICAL_MARKET_REACTION_V1.md).

Reproduce (offline once the price snapshot exists):

```bash
uv run python scripts/inspect_market_reaction_readiness.py fetch-prices --prices-dir <dir>   # NETWORK
uv run python scripts/inspect_market_reaction_readiness.py report --prices-dir <dir>
uv run python scripts/inspect_market_reaction_readiness.py build-fixture --prices-dir <dir> --snapshot-date <date>
```

No return outcome was read while producing any `tau` number below. Session assignment in the
inspection script is an estimate (regular 16:00 New York close, real session dates from the price
snapshot, early closes ignored); the production calendar belongs to MR-002.

## Verdict: READY WITH GAPS

`mr-v1` is honest and computable today for **NVDA and PFE only**, and for them only the
*clearly positive* regime reaches the verdict tier. Nothing found makes the methodology
fundamentally misleading, but two findings need a product-owner decision before `tau` is frozen
(§3 timestamp quality, §6 tail asymmetry).

## 1. Per-ticker summary

| | NVDA | PFE | AAPL | MSFT | AMZN |
|---|---|---|---|---|---|
| Stored article span | 2025-08-27 → 2026-09-13 | 2025-09-08 → 2026-09-13 | 2026-07-28 → 2026-08-31 | 2026-07-22 → 2026-08-21 | none |
| Real sentiment-scored articles | 1,703 | 1,149 | 344 | 146 | 0 |
| Demo / invalid-probability rows | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | – |
| Sessions with eligible news (after title dedup) | 240 | 223 | 24 | 22 | 0 |
| …of which ≥3 distinct sources | 138 | 134 | 22 | 14 | 0 |
| Median articles per signal session | 3.5 | 3 | 11.5 | 4 | – |
| Sessions first→last signal | 262 | 255 | 24 | 22 | 0 |
| ≥126-session history | yes | yes | no | no | no |
| Timestamp full / date-only / unusable | 37.1% / 62.9% / 0% | 14.4% / 85.6% / 0% | 77.3% / 22.7% / 0% | 61.0% / 39.0% / 0% | – |
| Signal sessions that are majority date-only | 223 / 240 | 208 / 223 | 10 / 24 | 15 / 22 | – |
| Adjusted price history (yfinance, 3y) | 753 sessions | 753 | 753 | 753 | 753 |
| In continuous coverage (`company_coverage`) | yes | yes | no | no | no |

All sentiment rows are `ProsusAI/finbert`. Monthly signal-session counts for NVDA and PFE are flat
(17–21 per month across the whole span), so the history is not front- or back-loaded.

## 2. Corpus lineage

Every stored article is Google News RSS: the live feed (`Google News RSS`) or the
`historical-range fallback`. GDELT has never succeeded (watermark: 4 consecutive `ConnectTimeout`
failures, zero rows). The year of history for NVDA/PFE is therefore 100% Google historical-range
backfill, which is relevance-ranked and capped per day (`_cap_articles_by_date`) — it is a sample
of what Google still surfaces today, not a record of what was published then.

## 3. Timestamp findings

- The historical-range feed stamps most entries at exactly **00:00:00 America/Los_Angeles**
  (07:00 UTC in PDT, 08:00 UTC in PST). 2,181 of 2,532 historical-range rows (86%) carry it; only 7
  of 810 live rows do. That is a date, not a time. It must be detected DST-aware — a hardcoded
  `07:00` misses every November–March row. Two live rows carry 00:00:00 UTC.
- 0% unusable: every `published_at` is timezone-aware UTC and none post-dates its `fetched_at`.
- `published_at` is stored as UTC built from feedparser's `published_parsed`, which is already
  UTC-normalised, so the full timestamps are trustworthy to the second/minute.
- **Consequence.** Under the approved conservative rule (date-only → end of local day → next
  session) ~93% of NVDA/PFE signal sessions are dominated by articles that were really published
  *during the previous calendar day*. No look-ahead leak results, which is the property the rule
  exists to protect. But "session 0" for those events is systematically the session *after* the
  news, so the day-0 "contemporaneous" marker understates the real same-day move and the +5 return
  starts one session late. This is a bias toward finding nothing, not toward false detection.
  The "local day" for a date-only stamp should be read as the Pacific date the feed reports.

## 4. Price and benchmark findings

- **Prices are not persisted.** There is no price table; `YFinancePriceProvider` fetches on
  demand and `CachingPriceProvider` holds them in process memory only. An `mr-v1` result is
  therefore not reproducible from the database alone.
- **The price field is genuinely split- and dividend-adjusted.** `sources/prices.py` calls
  `history(..., auto_adjust=True, actions=False)` and stores `Close` into `PricePoint.close`.
  Verified on data: auto-adjusted `Close` equals yfinance `Adj Close` exactly (max abs diff 0.0)
  for NVDA, PFE and ^GSPC; NVDA is continuous across its 10:1 split of 2024-06-10
  (120.54 → 121.44); PFE's raw/adjusted ratio three years back is 1.2128 (dividends folded in),
  NVDA 1.0030, AMZN 1.0000 (no dividends).
- Adjusted *levels* are rewritten backwards at every dividend, so a stored level is not comparable
  with a later fetch. Returns are unaffected. Frozen fixtures must be treated as snapshots.
- `PricePoint` carries a `date` only (the exchange-local session date). That is what `mr-v1` needs.
- `YFinancePriceProvider` requires ≥150 observations and defaults to a 3y period — fine for all
  five names and both indices.
- **No benchmark series exists anywhere** in the code or database (no `^GSPC`, `^FTSE`, or
  "benchmark" reference in `src/`).
- **The benchmark can reuse the current path unchanged.** `fetch()` only reads
  `constituent.yahoo_symbol`; `^GSPC` and `^FTSE` both return 3y of daily history through the
  identical call (753 and 761 sessions, no NaN closes). `^GSPC` session dates are identical to
  NVDA/PFE; `^FTSE` dates are identical to a `.L` listing (AZN.L checked).
- Both indices are **price-return** indices while the stocks are dividend-adjusted (total return).
  Over 5 sessions the mismatch is about 2–3 bp for the S&P 500 and 6–8 bp for the FTSE 100 —
  small against the 0.5% verdict floor, systematic in the stock's favour. A total-return proxy
  (adjusted `SPY` / `ISF.L`) removes it; choosing one is a benchmark-logic decision, not MR-001's.
- Largest one-session adjusted moves in the window (NVDA 18.7%, ^GSPC 9.5%, April 2025) are real
  market events, not feed artefacts.

## 5. Exchange-calendar findings

- `Constituent.market` (`"S&P 500"` / `"FTSE 100"`) plus `yahoo_symbol` (`.L` suffix) identifies
  the listing for every name in the universe, in both the built-in fallback list and
  `constituents_cache.json`. All five target tickers are `S&P 500` → US calendar, S&P 500
  benchmark. There is no explicit exchange/MIC field and no XNYS-vs-XNAS distinction; the two share
  one session calendar, so that is not a gap for V1.
- The `articles` table stores `ticker` only. Listing must be resolved through the constituent
  universe at compute time; a delisted or renamed ticker would be an "unresolved exchange".
- **No exchange-calendar capability exists.** No calendar dependency is in `pyproject.toml` /
  `uv.lock` and no session logic is in `src/`. Real price dates give holidays for free but not
  close times, early closes (e.g. 2025-11-28, 13:00 ET) or DST handling. A dependency such as
  `exchange_calendars` (XNYS, XLON) is the smallest way to meet §4 of the methodology.
- No London-listed company has any stored articles, so XLON alignment cannot be validated on real
  article data yet. The fixture includes real `^FTSE` sessions so calendar divergence is testable.

## 6. Session-signal distribution and event eligibility

Pooled over 509 signal-defined sessions (NVDA 240, PFE 223, AAPL 24, MSFT 22):

| quantile | 5% | 15% | 25% | 50% | 75% | 85% | 95% |
|---|---|---|---|---|---|---|---|
| `S_t` | −0.428 | −0.153 | +0.001 | +0.151 | +0.372 | +0.471 | +0.700 |

Mean +0.152. Article-level polarity has the same skew (16% ≤ −0.2, 39% ≥ +0.2).

**The distribution is strongly positive-skewed, so one symmetric `tau` cannot put ~15% of sessions
in each tail.** The 15% quantiles are −0.153 and +0.471; their symmetric average is 0.31.

| `tau` | pooled negative tail | pooled positive tail |
|---|---|---|
| 0.20 (floor) | 12.2% | 43.8% |
| 0.30 | 7.5% | 32.0% |
| 0.31 (symmetric average of the 15% quantiles) | 7.5% | 30.6% |

The procedure as written does not determine a unique `tau` here: the negative tail never reaches
15% at any `tau ≥ 0.20`, and the positive tail is two to three times the target. This is reported as
a contradiction for the product owner, not resolved here.

Plausible resolved event counts (≥3 sources, after +5 exclusivity, +5 path resolved):

| | NVDA neg | NVDA pos | PFE neg | PFE pos | AAPL neg / pos | MSFT neg / pos | AMZN |
|---|---|---|---|---|---|---|---|
| `tau` = 0.20 | 8 | 29 | 11 | 27 | 2 / 1 | 1 / 1 | 0 |
| `tau` = 0.31 | 2 | 23 | 8 | 20 | 2 / 1 | 1 / 1 | 0 |

Funnel at `tau` = 0.20 for NVDA positive: 117 tail sessions → 74 with ≥3 sources → 29 after
exclusivity. The exclusivity rule caps any regime near one event per 6 sessions, i.e. ≈42 per year
of history; the source rule removes roughly 40% of signal sessions on the thin backfill.

Names capable of each bar today:

| bar | count | names |
|---|---|---|
| ≥126 signal-history sessions | 2 of 5 | NVDA, PFE |
| ≥10 events in a regime | 2 of 5 | NVDA (pos), PFE (pos; neg only at `tau` = 0.20, n = 11) |
| ≥20 events in a regime | 2 of 5 | NVDA (pos), PFE (pos) — at `tau` = 0.31 PFE is exactly 20 |

No name reaches ≥20 negative events. AAPL/MSFT hold ~1 month; AMZN holds nothing.

## 7. Data-lineage risks that could mislead

1. **Date-only timestamps dominate the history** (§3). Safe against leakage, but day 0 is mostly
   "the session after the news". The day-0 label and copy must not imply same-session reaction for
   these events; the date-only share should be shown as a quality metric per company.
2. **Retrospective sampling.** The backfill is what Google surfaces a year later, relevance-ranked
   and per-day capped. Articles written *after* a price move ("why NVDA fell today") are in the
   corpus and, with date-only stamps, cannot be separated from articles that preceded it. Combined
   with (1) the next-session rule keeps this from leaking into the +5 return, but it can colour
   day 0.
3. **Two ingestion regimes in one series.** Before ~2026-08-17 sessions hold a median of ~3
   date-only backfill articles; after it, live ingestion gives many full-timestamp articles per
   session. Session means over more articles shrink toward zero (|S| ≥ 0.2 in 67% of 3–5-article
   sessions vs 46% of 11+-article sessions), so tail membership drifts with coverage depth, and a
   chronological split-half test partly compares ingestion regimes.
4. **Positive skew of FinBERT on headlines** (§6) makes "clearly positive" a common state (31–44% of
   sessions), which weakens its meaning as an event.
5. **Headline-only sentiment.** Polarity is scored from title/snippet, by a single model version;
   there is no sentiment-model version pin in the `mr-v1` inputs beyond `model_name`.
6. **Non-persisted, retro-adjusted prices** (§4): results are not reproducible from stored data.
7. **Price-return benchmark vs total-return stock** (§4): small, systematic, one-directional.
8. **Relevance noise.** The corpus admits some non-company items (e.g. "Pfizer Tokenized Stock
   price today … PFEX to USD"). They are scored and would enter `S_t`. "Belongs to the company" in
   `mr-v1` currently means only `articles.ticker`.

Helpful, not a risk: `articles.normalized_title` already exists (lower-cased, punctuation stripped,
publisher suffix already removed — 5 of 3,342 titles still end in " - <source>"), so the
same-session title dedup can reuse it rather than invent a second normaliser.

## 8. Fixture

`tests/fixtures/market_reaction/nvda_pfe_real_sample.json` (≈340 KB), pinned by
`tests/test_market_reaction_fixture.py`.

- 383 real NVDA/PFE articles: id (fingerprint), title, normalised title, source, provider,
  `published_at`, `fetched_at`, timestamp quality, FinBERT probabilities, model name. No URLs, no
  snippets, no secrets. Up to 6 per ticker per UTC day, deterministic order.
- Two windows: 2025-10-27 → 2025-12-01 (date-only backfill; US DST change 2025-11-02; Thanksgiving
  closure and the 2025-11-28 early close) and 2026-08-17 → 2026-09-13 (live full timestamps; Labor
  Day closure).
- 974 real adjusted closes: NVDA, PFE, ^GSPC, ^FTSE from 2025-10-01 to 2026-09-18, leaving exactly
  5 sessions after the last article — late events resolve at +5 but stay *pending* at +10.
- Carries no expected `mr-v1` outputs. It is input data, not a gold set.

## 9. Smallest enabling changes

1. Add an exchange-calendar dependency (XNYS + XLON) — needed for close times, early closes, DST.
2. Add S&P 500 / FTSE 100 benchmark fetch through the existing `PriceProvider` path (a benchmark
   pseudo-constituent with `yahoo_symbol="^GSPC"` / `"^FTSE"`); decide price-return vs total-return
   proxy.
3. Classify timestamp quality with the DST-aware Pacific-midnight rule and expose the share.
4. Product-owner decision on the `tau` procedure given the skew (§6) before MR-003 freezes it.
5. To make AAPL/MSFT/AMZN usable: a historical backfill (network + FinBERT scoring, no LLM needed
   for `mr-v1`). Not run here — it writes to the database and needs explicit approval.
6. Optional but recommended: persist the price/benchmark snapshot used for a computed result (or
   its fetch date) so a displayed statistic is reproducible.

## 10. Still unvalidated

- Whether Google's midnight-Pacific stamp is the article's true Pacific publication *date* or a
  coarser approximation (no second source to cross-check; GDELT never succeeded).
- XLON alignment and FTSE benchmarking on real London-listed articles (none stored).
- Early-close handling (the estimate here ignores it).
- How much relevance noise (§7.8) sits inside tail sessions; not measured.
- Event counts are estimates from a simplified calendar; MR-002's implementation may differ by a
  few events per regime.
- yfinance availability/limits on the deployed host for two extra benchmark symbols.
