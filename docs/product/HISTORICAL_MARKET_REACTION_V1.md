# Historical Market Reaction V1 (`mr-v1`)

Status: approved design; numeric sentiment threshold to be frozen during validation  
Owner: Alfred Hong  
Scope: deterministic historical research layer for MarketSentinel  
Methodology version: `mr-v1`

## 1. Product question

> When the news about this company was clearly positive or clearly negative, what did the stock do on that news session and over the following week relative to its market, and how consistent was that historically?

This is a historical association / event-study feature. It is **not** a forecast, causal estimate, buy/sell signal, or claim of alpha.

Longer-term purpose:

```text
current event
-> classify event
-> retrieve comparable historical events
-> show benchmark-relative historical reactions
-> link every observation to source evidence
```

`mr-v1` builds the quantitative foundation for that later analogue product.

---

## 2. Article signal

For each eligible article:

```text
article_polarity = p_positive - p_negative
```

Range: `[-1, +1]`.

Eligible articles must:
- belong to the company;
- not be demo data;
- have valid sentiment probabilities;
- have a usable publication date/time.

Do not require full paid LLM analysis. `mr-v1` should use the broader sentiment-scored corpus.

---

## 3. Deduplication

Apply both:

1. existing canonical article/URL deduplication;
2. deterministic title-normalised deduplication within the same assigned market session.

Title normalisation should be simple, versioned and deterministic. It may lower-case, strip publisher suffixes where safely identifiable, remove non-alphanumeric noise, and collapse whitespace.

Do **not** multiply sentiment by raw article count. Preserve article count and distinct-source count as context fields.

Cross-headline semantic event clustering is deferred.

---

## 4. Information-time alignment

An article is assigned to the **first listing-exchange trading session whose close occurs after publication**.

Examples:
- published during Monday US session -> Monday;
- published Monday after US close -> Tuesday;
- weekend/holiday publication -> next trading session.

Use exchange-aware, timezone-aware session calendars. Do not hardcode a global close time.

Supported V1 listing calendars must cover:
- US listings (XNYS/XNAS session calendar as appropriate);
- London listings (XLON).

Handle:
- holidays;
- early closes;
- daylight-saving changes;
- US/UK DST divergence.

If publication has a date but no trustworthy time-of-day, use the conservative rule defined by implementation tests: treat it as end-of-local-day so it cannot leak into an earlier session.

The implementation must expose timestamp-quality metrics.

---

## 5. Session signal

For trading session `t`:

```text
S_t = mean(article_polarity for deduplicated eligible articles assigned to t)
```

Also store:
- deduplicated article count;
- distinct source count;
- article IDs used.

A session with no eligible articles has **no signal**. It is not sentiment zero.

---

## 6. Positive / negative regimes

Two regimes:

```text
clearly_negative: S_t <= -tau
clearly_positive: S_t >= +tau
```

and require:

```text
distinct_sources >= 3
```

### Threshold rule

The **selection procedure** is fixed under `mr-v1`:

- choose `tau` from pooled session-sentiment marginals only;
- target approximately 15% of signal-defined sessions in each tail;
- enforce `tau >= 0.20`;
- do not inspect future-return outcomes while selecting `tau`.

The numeric `tau` is frozen during MR-003 validation **before outcome inspection** and becomes immutable for `mr-v1`.

Changing the numeric threshold after that requires a new methodology version.

No per-company threshold tuning.

---

## 7. Prices and benchmarks

Use split/dividend-adjusted prices.

For event session `t` and forward horizon `h`:

```text
stock_return_h = P[t+h] / P[t] - 1
benchmark_return_h = Q[t+h] / Q[t] - 1
market_adjusted_return_h = stock_return_h - benchmark_return_h
```

Benchmark mapping:
- US listing -> S&P 500 benchmark/proxy;
- London listing -> FTSE 100 benchmark/proxy.

Use the existing price path where possible. Benchmark data must use compatible session dates.

Missing required stock or benchmark prices are never imputed for an event.

---

## 8. Horizons

Primary confirmatory horizon:

```text
+5 trading sessions
```

Displayed descriptive path:

```text
news session (0), +1, +2, ... +10 sessions
```

The product does **not** search horizons and report whichever looks strongest.

Day 0 is contemporaneous context and must be labelled accordingly. It is not predictive evidence.

Optional +1 and +10 descriptive markers may be shown, but only +5 drives the main verdict.

---

## 9. Event-window exclusivity

To reduce dependence from overlapping forward-return windows:

- process events chronologically within each regime;
- keep the first qualifying event;
- suppress subsequent same-regime events whose primary +5 window overlaps the kept event.

Cross-regime overlap may remain if the regimes are mutually exclusive in practice.

Events without a complete required reaction path are pending/unresolved and excluded from resolved statistics.

---

## 10. Primary statistics

For each regime at +5 sessions, compute:

- resolved event count `n`;
- median market-adjusted return;
- mean market-adjusted return;
- 95% bootstrap confidence interval for the mean;
- share of events with market-adjusted return > 0.

Bootstrap:
- percentile bootstrap;
- deterministic fixed seed derived from methodology version;
- default `B = 2000`, unless validation demonstrates a compelling implementation reason to change it before `mr-v1` is frozen.

Do not use p-values in the user-facing V1.

---

## 11. Evidence states

### Not enough history
Use when the company does not meet the minimum history span required for defensible research.

Target rule from lead design:

```text
>= 126 trading sessions between first and last signal-defined session
```

MR-003 must verify this is practical on real data before final freeze.

### Hidden / insufficient regime
`n < 10`

Do not show a tiny-event distribution as if it were evidence.

### Preliminary
`10 <= n < 20`

Show descriptive evidence but no relationship verdict.

### Eligible for verdict
`n >= 20`

A regime may be labelled as historical relationship detected only if all are true:

1. `n >= 20`;
2. bootstrap 95% CI for the mean excludes zero;
3. `abs(mean) >= 0.5%`;
4. chronological first-half and second-half means have the same sign;
5. **mean and median have the same sign**.

If (1)-(3) pass but stability or mean/median agreement fails:

```text
status = unstable / outlier-sensitive
```

Otherwise:

```text
No consistent subsequent move detected.
```

The exact numeric thresholds above are frozen design inputs and may only be changed before `mr-v1` is formally frozen during MR-003.

---

## 12. Extreme returns and data integrity

Do not automatically discard an observation only because its return is large.

Large stock/benchmark returns must be:
- flagged as integrity-review candidates;
- checked for split/corporate-action/feed problems;
- excluded only when there is evidence the observation is invalid.

Real extreme market events belong in the sample.

Median must always be shown alongside mean so users can see outlier sensitivity.

---

## 13. Missing-data and quality states

Track at minimum:
- unusable/missing publication-time share;
- stock-price gaps;
- benchmark-price gaps;
- unresolved exchange;
- unresolved event windows.

The feature must support:

```text
not_enough_history
preliminary
no_consistent_relationship
unstable
data_quality_inadequate
detected
```

MR-001 and MR-003 may recommend exact data-quality thresholds, but they may not silently weaken leakage protections.

---

## 14. Secondary statistic

Optional methodology-drawer statistic:

- Spearman rank correlation between session sentiment and +5 market-adjusted return;
- only when there are enough signal-defined sessions (lead-design target: >=100);
- descriptive only;
- never used for the headline verdict.

If uncertainty is reported for this statistic, use a dependence-aware method such as block bootstrap.

---

## 15. Placebo and stability validation

Before public deployment, MR-003 must run:

- synthetic null tests;
- permutation/placebo tests;
- one-session lag-shift sensitivity;
- manual event face-validity inspection on representative companies;
- split-half stability checks.

Do **not** publish an assumed false-positive rate.

Measure the actual placebo/detection behavior for the final `mr-v1` procedure and document it.

---

## 16. User-facing surface

Section name:

# Historical Market Reaction

V1 should contain:

1. concise deterministic verdict/copy for positive and negative regimes;
2. reaction-path chart from day 0 through +10 with uncertainty;
3. event/outcome dots at +5;
4. compact statistics table;
5. event -> underlying article evidence drill-down;
6. short visible methodology disclaimer plus expandable detail.

Every dot/event should be auditable to the article IDs that created the session signal.

---

## 17. Claims policy

Allowed language:
- historically associated with;
- historical market reaction;
- subsequent return;
- benchmark-relative / market-adjusted;
- no consistent subsequent move detected;
- preliminary;
- unstable;
- insufficient history;
- descriptive, not causal.

Do not say:
- predict / forecast / expect / will;
- buy / sell / hold;
- alpha / edge;
- caused / drove;
- priced in by the close;
- strongest horizon;
- statistically significant;
- AI predicts;
- guaranteed.

Null copy should be:

> No consistent subsequent move detected after the news session.

Not:

> The news was priced in by the close.

---

## 18. Deferred from V1

Do not add to `mr-v1`:
- event-category conditioning;
- semantic event clustering;
- sentiment-surprise / change features;
- attention -> volatility analysis;
- sector/factor adjustment;
- 20-session primary horizon;
- rolling/regime analytics;
- predictive trading backtests;
- LLM-written statistical conclusions;
- current-event analogue matching;
- alerts/watchlists.

Those build on this foundation later.

---

## 19. Change-control rule

This document is the methodology source of truth.

Implementation agents may:
- clarify code-level details;
- add tests;
- report contradictions.

They may **not** silently change:
- signal formula;
- timing semantics;
- threshold selection procedure;
- benchmark logic;
- primary horizon;
- evidence/verdict rules;
- claims policy.

A consequential methodology change requires:
1. explicit Alfred approval;
2. entry in `docs/DECISIONS.md`;
3. a new methodology version if already frozen.
