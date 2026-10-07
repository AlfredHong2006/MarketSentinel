# Historical Market Reaction V1 (`mr-v1`)

Status: approved design; numeric sentiment thresholds to be frozen during validation  
Amended: 2026-09-19 (principal methodology review — asymmetric thresholds, day-0 timing cohort,
history sufficiency, benchmark instruments); 2026-10-07 (company-role eligibility, section 2.1; Student-t interval and zero-width rule, sections 10 and 11)  
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
- have a usable publication date/time;
- carry a stored company-role label of `principal` (section 2.1).

Do not require full paid event analysis (Stage A/B/C). `mr-v1` uses the broader sentiment-scored
corpus as its population: every sentiment-scored article is labelled, and the label, not the event
analysis, decides eligibility.

### 2.1 Company role

Each article carries one stored, versioned, immutable label for the covered company's role in the
development it reports:

- `principal`: the company is a party to the underlying event (buyer, seller, bidder, target,
  plaintiff or defendant, contractual counterparty, regulated or investigated entity, or owner of the
  affected asset, right, or liability);
- `mentioned`: the company is only context for someone else's development.

The label is an extraction recorded by a separate paid LLM stage with its own prompt and schema
versions. It is never a guess: an article that could not be labelled is **unlabelled**, which is a
different state from `mentioned`.

Eligibility is a deterministic rule on the stored label, never a prompt: an article counts only when
its label is `principal`. An unlabelled article does not count. The result must report how many
articles were excluded by the rule and how many were unlabelled, and which label contract (model,
prompt version, schema version) produced the labels used.

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

### Timing metadata

The conservative rule protects against leakage but systematically shifts date-only articles one
session late, so each session signal and each event must additionally carry:

```text
date_only_share    fraction of the session's kept articles with no trustworthy time
timing_class       exact | lagged
```

```text
exact  = date_only_share == 0   (every kept article had a full timestamp)
lagged = otherwise
```

One date-only article is enough to make a session `lagged`: its assigned session is then very
likely the one *after* the news rather than the one that reacted to it.

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

Two regimes, each with **its own threshold**:

```text
clearly_negative: S_t <= -tau_negative
clearly_positive: S_t >= +tau_positive
```

and require:

```text
distinct_sources >= 3
```

### Threshold rule

The **selection procedure** is fixed under `mr-v1`.

Selection population:

```text
E = pooled session signals with distinct_sources >= 3,
    built from principal-labelled articles only (section 2.1)
```

Sessions that could never qualify as events do not shape the tails.

```text
tau_positive = max(0.20, round(Q0.85(S | E), 2))
tau_negative = max(0.20, round(-Q0.15(S | E), 2))
```

Quantiles are linear-interpolated. Thresholds are rounded to two decimals.

Rules:

- select from pooled session-sentiment marginals only;
- never inspect future-return outcomes while selecting either threshold;
- the floor applies to each threshold independently;
- **neither threshold may be loosened to recover event counts.** A floored tail simply yields
  fewer events.

Rationale for asymmetry: real company-news sentiment is skewed, so one symmetric threshold puts
materially different shares of sessions in the two tails and silently makes one regime a much
rarer, more extreme event class than the other. Cutting each tail on its own side keeps the two
regimes comparable.

Both numeric thresholds are frozen during MR-003 validation **before outcome inspection** and
become immutable for `mr-v1`. Changing either afterwards requires a new methodology version.

The result must record both thresholds, both observed tail shares, and whether the pair is
provisional or frozen.

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
- US listing -> `SPY`;
- London listing -> `CUKX.L`.

These are investable trackers whose adjusted closes fold in dividends the same way the stock's
adjusted closes do, so a market-adjusted return subtracts like for like. The bare price indices
`^GSPC` and `^FTSE` drop the benchmark's dividend yield and would bias every market-adjusted
return upward by roughly that yield over the holding window; they are retained only as
validation/reference series where already present, never as the engine's benchmark input.

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

### Day-0 cohort

Day 0 is the only horizon whose meaning depends on the session being the *right* session, so:

- the day-0 aggregate uses **exact-timing events only**;
- no `lagged` event may contribute to it;
- it exposes its own `n_day0`, separate from the resolved event count;
- horizons +1 through +10, and the primary +5 statistic, continue to use **all** resolved events,
  lagged included — their anchor close is genuinely after publication either way.

Because day 0 and +1..+10 may therefore be computed over different cohorts, the result contract
must state each horizon's cohort and its own `n` explicitly. A consumer must never have to infer
that the sample sizes are identical. Where no exact event exists, day 0 is absent rather than
approximated.

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
- an approximate 95% interval for the mean (below);
- share of events with market-adjusted return > 0.

Interval for the mean:
- classical Student-t interval, `mean +/- t(0.975, n-1) * s / sqrt(n)` with `s` the sample standard
  deviation (n-1 divisor). It is deterministic and uses no random seed or resampling.
- it is approximate, not an exact 95% interval. MR-008 measured how often it excluded zero on
  simulated mean-zero returns (standard deviation 4.31%, 8,000 trials per cell, Monte-Carlo
  standard error about 0.25 percentage points) for n = 20-50:
    - normal: 4.9-5.3%;
    - heavy-tailed (Student-t, 3 degrees of freedom): 4.2-4.6%;
    - contaminated (90% small moves, 10% five times larger): 3.6-4.2%;
    - skewed (exponential, skewness 2): 6.4-8.0%.
  The measured rate is above the nominal 5% for skewed returns and below it for heavy-tailed ones.
  Real market-adjusted returns were not used to choose the method and may behave differently.
  Do not describe the interval as having exactly 95% coverage or a 5% false-positive rate.
- the same method is used at every horizon, including day 0.
- zero-width rule: if the interval cannot be computed from the sample (fewer than two events,
  a non-finite value, no spread among the values) or its lower and upper bounds are equal, the
  interval is reported as degenerate, spans zero, and never counts as excluding zero. This holds
  for any interval method and is checked both when the interval is built and when the verdict is
  applied.
- the percentile bootstrap, bootstrap-t and BCa remain selectable in code for research, with
  seeds derived from the methodology version; they are not used for a verdict.
- the result records the interval method; the bootstrap resample count is reported only for a
  method that resamples.

Do not use p-values in the user-facing V1.

---

## 11. Evidence states

### Not enough history
Use when the company does not meet the minimum history required for defensible research.

All three conditions are required (inclusive):

```text
span      = last_signal_index - first_signal_index          >= 126
density   = N_signal / (span + 1)                           >= 0.50
coverage  = N_eligible                                      >= 63
```

where `N_signal` is the number of signal-defined sessions and `N_eligible` is the number of
signal-defined sessions with `distinct_sources >= 3`.

Span alone is not sufficient: two dense clusters of news separated by a long silence produce a
wide span over a corpus that cannot support an event study. Density measures coverage of the
window itself, and `N_eligible` ensures enough of those sessions could actually become events.

The result must report all three observed quantities alongside their thresholds.

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
2. the interval for the mean (section 10) excludes zero, and is not degenerate or zero-width;
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
- share of articles without a role label;
- share of articles excluded as `mentioned`;
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
- the company-role eligibility rule, its vocabulary, or the unlabelled-article policy;
- timing semantics, including the day-0 cohort rule;
- threshold selection procedure, including the asymmetry and either floor;
- benchmark logic, including the benchmark instruments;
- primary horizon;
- evidence/verdict rules, including the three history-sufficiency conditions;
- the interval method for the mean or its zero-width rule;
- claims policy.

A consequential methodology change requires:
1. explicit Alfred approval;
2. entry in `docs/DECISIONS.md`;
3. a new methodology version if already frozen.
