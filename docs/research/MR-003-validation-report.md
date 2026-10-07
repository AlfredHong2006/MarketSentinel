# MR-003 — `mr-v1` real-data validation report

Status: **BLOCKED pending Alfred's entity-filter decision (follow-up §F1.3, §F6); pooling (A), tau_positive 0.42 is recorded as provisional.** Everything not dependent on it is done and measured;
the positive regime's outcomes were deliberately not computed. Sections were written in phase order;
each states what had and had not been loaded when it was written.

Scope (DECISIONS 2026-10-06): NVDA and PFE only. No backfill was run. **The London (LSE) path is
untested in validation**: no London-listed company has stored articles, so XLON calendar alignment
and `CUKX.L` benchmarking on real London articles were not exercised. Nothing in this report should
be read as validating the London path, including any `CUKX.L` calendar/alignment check.

## 1. Phase 1 — threshold pre-registration (sentiment only)

### 1.1 How outcome-blindness was enforced

Written at 2026-10-06 21:48 UTC, before any price, return, or event outcome was loaded.

- `scripts/mr003_preregister_thresholds.py` is the only code run in this phase. It opens
  `C:\Dev\MarketSentinel\data\marketsentinel.db` with SQLite URI `mode=ro`, reads only `articles`
  and `sentiments`, and writes nothing to it.
- The session calendar is built from article dates alone (US/XNYS, 2025-08-13 → 2026-10-16), never
  from a price series.
- The script imports no price provider or return path and aborts if `yfinance` is already in
  `sys.modules`; it records `price_modules_loaded: false` in its output.
- `select_thresholds` takes session signals and nothing else, so no return can reach it.
- At the time of computation `tests/fixtures/market_reaction/` held only the frozen
  `nvda_pfe_real_sample.json`; the new tracker-series fixture did not yet exist and no price fetch
  had been run.

### 1.2 Population and snapshot

Database snapshot: last written 2026-09-13 17:18 UTC, `PRAGMA user_version = 5`. Pooled over the
four companies with stored real sentiment-scored articles (509 signal-defined sessions; AMZN has
none).

| company | real scored articles | signal sessions | contribution to E (≥3 sources) | span | density | history sufficient |
|---|---|---|---|---|---|---|
| AAPL | 344 | 24 | 22 | 23 | 1.000 | no (span, coverage) |
| MSFT | 146 | 22 | 14 | 21 | 1.000 | no (span, coverage) |
| NVDA | 1,703 | 240 | 123 | 261 | 0.916 | yes |
| PFE | 1,149 | 223 | 134 | 254 | 0.875 | yes |

### 1.3 Thresholds, both ways

Selection: `tau_positive = max(0.20, round(Q0.85(S|E), 2))`, `tau_negative = max(0.20,
round(-Q0.15(S|E), 2))`, linear-interpolated quantiles.

| | (A) E = all four companies | (B) E = history-sufficient only (NVDA, PFE) |
|---|---|---|
| population `|E|` | 293 | 257 |
| raw Q0.85 → `tau_positive` | 0.42 → **0.42** | 0.44 → **0.44** |
| raw −Q0.15 → `tau_negative` | 0.15 → **0.20** (floor applied) | 0.14 → **0.20** (floor applied) |
| positive tail share (of E) | 15.36% | 15.18% |
| negative tail share (of E) | 10.58% (floor, short of 15%) | 10.12% (floor, short of 15%) |

The negative threshold is the 0.20 floor under both poolings, so it is not in dispute. The positive
threshold differs after rounding (0.42 vs 0.44).

### 1.4 Escalation

The spec is silent on whether companies failing history sufficiency belong in `E`. DECISIONS
2026-10-06 and the MR-003 packet require escalation when the rounded thresholds differ, and forbid
choosing the pooling that yields more events. This is that case. **Alfred must choose (A) or (B)**
(see §7). Neither was selected here.

Consequences handled in this run:

- The negative regime (0.20 under both) and all price-free work (history sufficiency, the event
  funnel up to the +5 price-resolution step, synthetic null, fixture, harness, result contract) are
  unblocked and proceed.
- **No positive-regime return, evidence state, placebo or lag-shift result has been computed or
  looked at.** This keeps the choice between 0.42 and 0.44 outcome-blind for Alfred.
- `MR_V1_FROZEN_THRESHOLDS` is **not** set; it depends on this decision.

## 2. Phase 2 — real inputs

One network read, performed by `scripts/mr003_fetch_prices.py` through the existing price path
(`YFinancePriceProvider`, `auto_adjust=True`, 3y daily), after §1 was written. Frozen as
`tests/fixtures/market_reaction/nvda_pfe_tracker_prices.json` (adjusted closes only):

| symbol | role | sessions | range |
|---|---|---|---|
| NVDA, PFE | validated stocks | 751 each | 2023-10-09 → 2026-10-06 |
| SPY | US benchmark (the engine's input) | 751 | 2023-10-09 → 2026-10-06 |
| CUKX.L | London benchmark, **alignment check only** | 757 | 2023-10-06 → 2026-10-06 |

`nvda_pfe_real_sample.json` (index series) is untouched. Adjusted levels are rewritten at every
dividend, so the fixture is a snapshot: compare returns, never levels, across fetches. Prices are
still not persisted anywhere (DECISIONS 2026-10-06: deferred to MR-004).

`tests/test_market_reaction_tracker_fixture.py` pins structure and alignment (six offline tests, no
outcome asserted): NVDA, PFE and SPY share one session calendar exactly and sit on XNYS with zero
off-calendar dates and zero gaps; every `CUKX.L` date is an XLON session.

**London (LSE) path: untested in validation.** The `CUKX.L` check proves only that the tracker's dates
are XLON sessions. No London-listed company has stored articles, so XLON information-time assignment,
the Europe/London close and DST handling on real articles, and `CUKX.L` as a benchmark for a London
stock were not exercised.

## 3. Phase 3 — measurement

`scripts/mr003_validate.py` (explicit thresholds, DB read-only, fixture prices, no network). Output
reproduced byte-for-byte on a second run. Threshold used for the negative regime: **0.20** (identical
under both poolings). The positive regime was run with `--regimes none`: funnel counts only.

### 3.1 History sufficiency (observed vs required)

| | span ≥ 126 | density ≥ 0.50 | coverage ≥ 63 | result |
|---|---|---|---|---|
| NVDA | 261 | 0.916 (240 / 262) | 123 | sufficient |
| PFE | 254 | 0.875 (223 / 255) | 134 | sufficient |
| AAPL | 23 | 1.000 | 22 | fails span, coverage |
| MSFT | 21 | 1.000 | 14 | fails span, coverage |

The three-condition rule is practical on this corpus: it admits exactly the two companies with a year
of history and rejects the two with about a month, with margin on both sides (NVDA/PFE clear the
coverage floor by about 2x). Density never binds here; AAPL/MSFT have density 1.0 over a 3-week window,
which is why span and coverage are both needed.

### 3.2 Event funnel (price-free counts; both candidate positive thresholds)

tail sessions → ≥ 3 sources → after +5 exclusivity → resolved at +5. Every event resolved; none
pending or missing a price.

| | tail | ≥ 3 src | after excl. | resolved | exact-timing resolved |
|---|---|---|---|---|---|
| NVDA negative (0.20) | 21 | 9 | 7 | 7 | 0 |
| PFE negative (0.20) | 35 | 17 | 12 | 12 | 0 |
| NVDA positive, 0.42 / 0.44 | 45 / 42 | 19 / 16 | 15 / 14 | 15 / 14 | 1 / 1 |
| PFE positive, 0.42 / 0.44 | 49 / 46 | 24 / 23 | 15 / 14 | 15 / 14 | 0 / 0 |

The ≥ 3-source rule removes 50–60% of tail sessions, then exclusivity up to about 40% more (PFE
positive). These counts are well below the MR-001 estimates (e.g. NVDA positive 29 at τ = 0.20), which
used a simplified calendar.

**No regime reaches n = 20 under either pooling.** The maximum is 15. `detected` and `unstable` are
therefore unreachable for NVDA and PFE with the current corpus, and the 0.42 / 0.44 choice cannot move
any regime across an evidence-state boundary (both give `preliminary`, n 14–15).

### 3.3 Engine result, negative regime (the only regime whose outcomes were computed)

| | n | n_day0 | state | mean | median | 95% CI | split-half (1st / 2nd) |
|---|---|---|---|---|---|---|---|
| NVDA | 7 | 0 | `insufficient_events` | −0.08% | −0.72% | [−1.98%, +1.96%] | −0.70% / +0.38% |
| PFE | 12 | 0 | `preliminary` | +0.10% | +1.28% | [−2.15%, +2.06%] | −0.17% / +0.38% |

Neither CI excludes zero; neither meets the 0.5% effect floor; both split-halves disagree in sign.
The +1…+10 path (all-resolved cohort, same n at every horizon) is in the script output; no horizon
was searched for a best one and none drives anything. **Day 0 is absent for both**: every resolved
event is `lagged` (100%), so the exact-timing cohort is empty.

Secondary Spearman (sentiment vs +5 market-adjusted return over all signal sessions): NVDA ρ = −0.113
(n = 240), PFE ρ = −0.036 (n = 223); descriptive only. *Disclosure:* the engine computes this over
every session, so it was seen while reviewing the negative output. It spans both tails and is not a
regime outcome, but it is a faint hint of the positive regime's direction and Alfred should know it
was seen.

### 3.4 Spec §15 validation — measured

**Synthetic null** (iid mean-zero market-adjusted +5 returns, σ = 4.31%, the mean unconditional NVDA/PFE
σ; 2,000 trials each). Share labelled `detected`:

| n | normal, no effect | Student-t(3), no effect | normal, +2% true mean | t(3), +2% true mean |
|---|---|---|---|---|
| 20 | 6.6% | 9.2% | 55.8% | 67.9% |
| 30 | 6.7% | 7.1% | 71.9% | 80.1% |
| 50 | 5.8% | 7.0% | 90.5% | 91.0% |

On pure noise the full verdict rule labels about 6–9% of samples `detected`, against 5% nominal for the
bootstrap interval alone: the percentile bootstrap is anticonservative at small n and heavy tails.
This is the measured behaviour of the procedure; no false-positive rate should be published as
assumed.

**Random-session placebo** (n = 20 non-overlapping sessions drawn from each company's own signal span,
1,000 draws, full verdict rule): NVDA 3.9% detected, PFE 2.6% detected. Real returns give lower rates
than the iid synthetic case, so the iid figure is the conservative one.

**Signal permutation on real event sessions** (shuffle session sentiment among ≥ 3-source sessions,
keep prices, thresholds, exclusivity and the verdict rule; 1,000 draws, negative regime): 0.0% reach
n ≥ 20 (median n 8 NVDA, 13 PFE), so measured placebo detection is **0%**, structurally: the procedure
cannot detect anything at these event counts, real or placebo. This is a statement about power, not
evidence of calibration; the two figures above are the calibration check.

**One-session lag shift** (re-anchor every signal one session later / earlier, rerun the procedure):

| | +1 mean (state) | −1 mean (state) | unshifted mean |
|---|---|---|---|
| NVDA negative | +1.01% (insufficient) | −1.09% (insufficient) | −0.08% |
| PFE negative | +0.83% (preliminary) | −0.75% (preliminary) | +0.10% |

Means move by about ±1% and change sign under a one-session shift while every CI spans zero. At n = 7
and 12 this is noise rather than a finding, but it shows the primary statistic is not stable to the
alignment error that date-only timestamps introduce.

**Split-half** (§3.3): sign disagreement in both regimes. The chronological split does not straddle
the ingestion break (§3.5), so it compares early-backfill halves only.

### 3.5 Data-lineage risks, quantified (negative regime)

- **Lagged-event share: 100%** (7/7 NVDA, 12/12 PFE). Corpus-wide, 62.9% (NVDA) and 85.6% (PFE) of
  articles are date-only, and 228/240 and 213/223 signal sessions are `lagged`. The day-0 cohort is
  empty for every regime tested; day 0 is in practice not producible from this corpus.
- **Two ingestion regimes:** 0 of 19 negative events fall on or after 2026-08-17 (live full-timestamp
  era). Live-era sessions hold many articles, whose means shrink toward zero, so the tails are populated
  by thin early-backfill sessions (median 4 articles per event). The straddle MR-001 feared does not
  occur for the negative regime; the live era is simply absent from it. Not assessed for positive.
- **Relevance noise inside tail sessions:** kept articles whose title names neither ticker nor company:
  0 of 35 (NVDA events) and 0 of 68 (PFE events), and 0 of 1,658 / 0 of 1,127 across all signal
  sessions. Quote-page / aggregator-pattern titles: 0 of 35 and 0 of 68 in event sessions (2 of 1,658
  and 8 of 1,127 overall). The bigger relevance problem is not off-topic items but **sentiment about
  the wrong party** (§3.6), which this proxy cannot see.
- **Issuer channels:** the engine collapses issuer-owned sources into one `official-company` voice,
  which still counts toward the ≥ 3 rule. 3 of 7 NVDA and 1 of 12 PFE negative events include it; 2 and 1
  respectively fall below 3 sources without it.

### 3.6 Face validity (negative regime; 6 events per company drawn with a fixed seed; article IDs are the first 12 hex characters of the stored fingerprint)

Verdict: **mixed, and the weakness is the sentiment signal rather than the event machinery.**

Plausibly company-negative: NVDA 2025-09-16 (China antitrust accusation; 03b5860ea35a, e0076bb6874e);
NVDA 2025-11-12 (SoftBank sells entire stake; 7b3f8ea17d25, ae15e28bc769, e2bfe7225637); NVDA 2025-11-21
(stock closes lower after earnings; 3636e7774cad); NVDA 2026-03-23 (smuggling charges; 4ed5641b3b24,
8a28cbe802bc); PFE 2025-09-19 (Arvinas programme dropped; 6309947293a1, f3c0d6d0955f); PFE 2025-11-03
(Metsera litigation; 3288421a0351, fd04bbd1ef88, c8785c837c04).

Misleading or off-target:
- PFE 2026-02-03, signal −0.918, the most extreme sample: three headlines about Sanofi being
  *reprimanded* over claims about a Pfizer vaccine (04bcd309de32, 79490951feb2, 9a0749fa1e20). Negative
  tone, but the party wronged is Sanofi; the +5 market-adjusted return that followed was +6.8%.
- NVDA 2025-12-05 (−0.22): four of six articles are NVIDIA developer-blog technical posts
  (168fd1d3db67, 1b1179cd5729, 96f2f76d24a7) with no market content.
- PFE 2026-07-20 (Pfizer HQ structural failure, patent suits; b0d8a0ca51d7, d8e910fb61fc,
  34b23392dde2) and 2026-01-20 (ViiV exit, Polish vaccine court case; 9aa302576148, c02f265332ea):
  neutral-to-mixed for an investor.
- Several stored titles carry mojibake (`�`); not a sentiment problem, but visible in any drill-down.

About 6 of 12 sampled events are cleanly company-negative news. This is the largest threat to
interpreting any eventual verdict: the forward-return test is only as meaningful as the event
definition.

## 4. Phase 4 — freeze proposal

`MR_V1_FROZEN_THRESHOLDS` is **not set**: it depends on Alfred's pooling decision (§7). Proposal once
decided: `RegimeThresholds(negative=0.20, positive=<0.42 or 0.44>)`, with tests pinning the constant,
asserting `resolve_thresholds(None)` reports `frozen`, and that the floor holds. The negative value is
not in question.

### 4.1 Result contract for MR-004 / MR-005 (`MarketReactionResult`, `market_reaction/models.py`)

Versioned, self-describing, frozen Pydantic. Fields: `methodology_version` (`mr-v1`),
`title_normalization_version`, `ticker`, `exchange` (`XNYS` | `XLON` | null), `benchmark_symbol`,
`thresholds{negative, positive}` (positive magnitudes), `threshold_status` (`frozen` | `provisional`),
`min_distinct_sources`, `primary_horizon` (5), `max_path_horizon` (10), `bootstrap_resamples`, `state`,
`signal_session_count`, `first/last_signal_session`, `history` (span / density / qualified count, each
with its minimum and a `meets_*` flag), `positive` and `negative` (`RegimeResult`), `session_signals`,
optional `spearman`, `data_quality`.

`RegimeResult`: `state`, `threshold`, counts (`qualifying`, `resolved`, `pending`, `missing_price`,
`suppressed`, `excluded`, `day_zero_event_count`, `exact_timing_event_count`), `primary` (`n`, `mean`,
`median`, `ci_low`, `ci_high`, `share_positive`), `path` (one `PathPoint` per horizon, each with its
own `cohort` and `statistics.n`), split-half means, the four verdict-rule booleans, and `events` (each
with `article_ids`, `sources`, `timing_class`, `date_only_share`, status, full 0..10 stock / benchmark /
market-adjusted returns, integrity flags).

Evidence states: `data_quality_inadequate` and `not_enough_history` (company-level; they override both
regimes), then per regime `insufficient_events` (n < 10), `preliminary` (10 ≤ n < 20),
`no_consistent_relationship`, `unstable`, `detected`.

A consumer must not infer:
- that day 0 and +1…+10 share a sample: day 0 is `exact_timing`, forward horizons `all_resolved`, each
  with its own `n`; **day 0 is absent whenever no exact event exists, which is every case in this
  corpus**;
- that `insufficient_events` means "no effect": it means no distribution may be shown;
- that `preliminary` carries a verdict: it never does;
- that a market-adjusted return is raw: it is stock minus `SPY` / `CUKX.L` over identical sessions;
- that thresholds are symmetric, or that `provisional` thresholds are the product's;
- that session sentiment is about the subject company (§3.6), or that a `lagged` event reacted on its
  own session;
- that a per-event day-0 value is displayable: `stock_returns[0]` / `market_adjusted_returns[0]` are kept
  on every event as provenance, but may be shown only when `timing_class` is `exact` (follow-up §F3);
- that the result is reproducible from the database: prices are not persisted;
- anything causal or predictive: the claims policy (spec §17) applies to every surface.

## 5. Summary of findings

1. The thresholds are not uniquely determined by the spec: positive 0.42 vs 0.44. Escalated.
2. The corpus cannot produce a verdict. Maximum resolved n is 15 per regime per company; `detected` is
   unreachable. This is independent of the escalation.
3. The engine behaves honestly: it shows `insufficient_events` / `preliminary`, nothing reaches a
   relationship claim, and measured detection is 0% under permutation at real n and 3–9% on noise at
   n = 20.
4. Date-only timestamps make every tested event `lagged` and empty the day-0 cohort.
5. The binding quality risk is headline sentiment about the wrong party, not off-topic relevance.

## 6. Assumptions still unvalidated

- The London path (XLON assignment on real articles; `CUKX.L` as a stock benchmark).
- The positive regime's outcomes, permutation, lag shift and split-half (not computed).
- Whether Google's midnight-Pacific stamp is the true Pacific publication date (no second source).
- Behaviour at n ≥ 20 on real data: no regime reaches it, so `detected` / `unstable` are exercised only
  by synthetic data and the existing unit tests.
- Price-feed stability on the deployed host (prices are not persisted).
- Early-close handling on real articles (delegated to the calendar library, not separately audited).

## 7. Decisions needed from Alfred

1. **Pooling for `E`** (blocks the freeze): (A) all companies with stored sentiment, giving positive
   0.42; or (B) history-sufficient companies only, giving positive 0.44. Both leave negative at the 0.20
   floor. Every regime's evidence state is the same either way (`preliminary`, n 14–15), so this changes
   event counts by one and the principle (does a company that cannot be analysed shape the thresholds for
   those that can?), not the verdict tier. (B) fits the spec's own logic that sessions which could never
   qualify do not shape the tails. Not selected here.
2. Whether to proceed given the corpus cannot reach n ≥ 20 (the backfill decision per DECISIONS
   2026-10-06 is deferred until this result).
3. Whether date-only timing and wrong-party sentiment (§3.5, §3.6) are acceptable for a public surface or
   call for a methodology change in a later version. No rule was changed here.

Reproduce: `scripts/mr003_preregister_thresholds.py`, `scripts/mr003_fetch_prices.py`,
`scripts/mr003_validate.py` (arguments in each docstring). After the decision, rerun the validator with
`--regimes both` and the chosen `--tau-positive`.


---

## Follow-up after Alfred's decisions (2026-10-06)

Status stays **BLOCKED**, now pending Alfred's decision on an entity filter (§F1.5). Alfred's recorded
decisions: pooling **(A)**, `tau_positive = 0.42`, `tau_negative = 0.20` as the **provisional** pair
(population: all companies with ≥ 3-source sessions); `MR_V1_FROZEN_THRESHOLDS` stays unset; the
positive-regime outcome measurement has **not** been run. Everything below is sentiment-only or
event-count-only. The one script that touches prices, `mr003_entity_relevance.py`, reads event *status*
counts only and prints no return, mean, interval, or evidence state; its face-validity sample prints
headlines and sentiment, no returns.

### F1. Entity relevance: does stored data identify the primary company?

**Answer: no field identifies the primary subject of an article.** What exists:

| candidate | what it is | coverage | tells us "company is primary"? |
|---|---|---|---|
| `articles.relevance_score` | title-only name-match score (`normalization.relevance_score`) | 100% (all 3,342 rows) | **No.** It measures *mention*: NVDA/PFE rows are only 0.85 (full company name in title) or 0.95 (plus a finance word); PFE has 8 rows at 0.60. A headline about Sanofi that names Pfizer scores the same as a Pfizer earnings story. |
| `article_intelligence_analyses.subject_company` | `CompanyReference` on each stored analysis | 188 of 1,703 NVDA and 114 of 1,149 PFE articles (about 10–11%) | **No.** Its docstring and schema say identity is *application-owned*: it is the ticker the article was fetched for, not a model judgement. |
| Stage A `EventExtraction` (`event_type`, `direction`, `magnitude`, channels) | LLM extraction | same ~10% | No role/principal field. |
| Stage C `related_companies` | other companies the event may affect | same ~10% | Indirect at best, and only on analysed articles. |
| `subject_principal.reads_as_third_party_appointment` | existing deterministic, title-only rule | runs on all titles | Only one narrow shape: the company named solely as a person's employer in someone else's appointment. By design (module docstring) it is not a general principal classifier. |

So there is no per-article "primary company" or relevance field with usable coverage, and the broader
sentiment-scored corpus `mr-v1` is specified to use has no paid analysis behind most of it.

#### F1.1 What the closest deterministic filters do (outcome-blind)

Filters applied to articles before session signals; thresholds recomputed from sentiment only on the
pooled (A) population; event counts are tail → ≥ 3 sources → after exclusivity → resolved (all resolved,
none pending). `scripts/mr003_entity_relevance.py`.

| variant | articles kept (NVDA / PFE / AAPL / MSFT) | `|E|` | `tau_positive` / `tau_negative` (raw) | NVDA neg / pos events | PFE neg / pos events |
|---|---|---|---|---|---|
| V0 none (provisional pair) | 1,703 / 1,149 / 344 / 146 | 293 | **0.42 / 0.20** (0.42 / 0.15) | 7 / 15 | 12 / 15 |
| V1 drop third-party appointments (the repo's only principal rule) | 1,703 / 1,145 / 344 / 146 | 292 | 0.42 / 0.20 (unchanged) | 7 / 15 | 12 / 15 |
| V2 `relevance_score ≥ 0.95` (name **and** finance word in title) | 578 / 262 / 43 / 33 | 78 | 0.44 / 0.20 (0.44 / 0.18) | 3 / 8 | 2 / 1 |

- **V1 changes nothing that matters:** four PFE articles are removed (0.35%), PFE loses one signal
  session, thresholds and every event count are identical. It is correct but far too narrow to address
  the wrong-party problem.
- **V2 is not a principal filter** (it keeps articles with finance words, not articles about the company),
  and it destroys the corpus: both NVDA and PFE then fail history sufficiency and event counts collapse.
  Reported only to show that `relevance_score` cannot stand in for a principal test. It is ruled out.
- Hence the recomputed thresholds on a *filtered pool* are, with the only filter that is principled,
  **unchanged: 0.42 / 0.20.** With V2 they would move to 0.44 / 0.20, which says nothing about a real
  principal filter.

No title-position rule was tried: `subject_principal.py` documents why ("Sanofi sues Pfizer" is a Pfizer
development) and that every rule there deletes a development, so such a rule is a product decision.

#### F1.2 Face validity of 12 sampled events after the V1 filter

Three events per company and regime (fixed seed; resolved events only). **Headlines and sentiment
only; no returns were read.** Article IDs are the first 12 hex characters of the stored fingerprint.
"Principal" is my manual judgement of whether the company is a party to the main development the
session's headlines report.

| # | company, regime, session (signal; articles) | principal? | basis |
|---|---|---|---|
| 1 | NVDA neg 2025-09-16 (−0.294; 4) | mostly yes | China antitrust accusation (03b5860ea35a, e0076bb6874e); plus a CoreWeave capacity deal and a developer-blog post |
| 2 | NVDA neg 2026-02-03 (−0.242; 3) | yes | shares fall on stalled OpenAI investment (3dd9db24bc47); two filler items (a developer blog, a "millionaire" listicle) |
| 3 | NVDA neg 2026-03-23 (−0.382; 4) | **no** | Supermicro co-founder charged with smuggling Nvidia chips (4ed5641b3b24, 8a28cbe802bc): Nvidia is the product, not a party; the others are a Bain essay and a Jacobs GTC item |
| 4 | NVDA pos 2025-10-16 (0.502; 3) | yes | HSBC upgrade (2ebf02ba64cd); an Arm newsroom item and a developer blog |
| 5 | NVDA pos 2026-02-19 (0.456; 4) | mixed | Nvidia sells entire Arm stake (f73c9c6f96af) yes; two developer-blog posts and a "trending tickers" roundup (27d93282e5f5, c92a7988ecaf, 97c1b5453a18) no |
| 6 | NVDA pos 2026-04-21 (0.495; 9) | **no** | rival's funding round, Nokia collaboration, a Forbes "Quantum strategy" piece, a developer blog, "Mag 7 excluding Nvidia" (007c0a6368a4, 056ab828069c, 17120a000c24, 30858f9e3ea1, 4637d06eb387): no single Nvidia development |
| 7 | PFE neg 2025-12-17 (−0.655; 14) | yes | 2026 guidance below estimates (0f4811063d93, 4506d0e701b5, 4e493a1bbb52, 6d69afbb2a63, 8928f1628731) |
| 8 | PFE neg 2026-01-20 (−0.598; 3) | yes | ViiV exit (9aa302576148); Polish vaccine court case (c02f265332ea); a stock-valuation explainer. Party to both, but "negative" is debatable for the ViiV sale |
| 9 | PFE neg 2026-06-22 (−0.246; 15) | mixed | Denmark study shutdown, CFO search, CFO departure (4cec7bbd48bf, 5f95b1f7faf5, 853c3a4965d7) yes; an MSD Prevnar approval (6c7035658a90) is a competitor's news |
| 10 | PFE pos 2025-11-20 (0.608; 3) | yes | mRNA flu shot beats comparator (55b21494c805, cc2fdcd19af4); research collaboration |
| 11 | PFE pos 2026-06-08 (0.619; 12) | yes | Chai licence agreement (0606376a4b34, 31ffb5a416ab), plus CEO interview and valuation pieces |
| 12 | PFE pos 2026-08-19 (0.452; 8) | mixed | CEO interview and an EMA Lyme filing validation (147a7297f9bd, b1a30782c5e5) yes; a contraception market report and an NVIDIA/Quantinuum story that names Pfizer in passing (9f4e26f35e91, 8f6030aac41a) no |

Tally: 6 clearly principal-driven (2, 4, 7, 8, 10, 11), 4 mixed (1, 5, 9, 12), **2 not principal-driven
(3, 6)**. V1 removed none of the 12 problem sessions; the problem sessions are not appointment-shaped.
Issuer-owned developer-blog posts appear in 5 of the 12 sessions (1, 2, 4, 5, 6) and recur in the pool
as filler that carries sentiment but no market information; they are already collapsed to one
`official-company` voice for the ≥ 3-source count, but their polarity still enters `S_t`.

#### F1.3 What a real filter would take

No deterministic title rule recognises the two failure shapes seen here ("X is reprimanded over claims
about Pfizer's vaccine"; "Supermicro drops after Nvidia-chip smuggling"), and a position-based rule
would delete genuine developments (per `subject_principal.py`). Practical options:

1. **A small, separately versioned role-extraction stage** (principal / counterparty / merely mentioned),
   run on the articles whose labels can change the result. For the thresholds the filtered pool `E` must
   be recomputed, so every article in a ≥ 3-source session needs a label: **2,835** articles in the
   current corpus (NVDA 1,423, PFE 957, AAPL 327, MSFT 128); only the **529** in tail sessions would
   need labels to filter *events*, but the thresholds then could not be recomputed outcome-blind from
   the same pool. Rough input is a few hundred tokens per headline-plus-snippet call, about 1 million
   tokens for the full pool, i.e. small-model list-price cost in the low single dollars or less (I have
   not looked up current prices; an estimate, no call was made).
2. **Why that is not a code-only change:**
   - the amended spec says `mr-v1` "should use the broader sentiment-scored corpus" and "Do not require
     full paid LLM analysis" (§2); an LLM-derived eligibility filter contradicts that and is a
     methodology amendment (DECISIONS entry) or a later version;
   - the result must be persisted to be reproducible (extraction is a stored fact, invariant 2): a new
     table is a **SQLite schema / `user_version` change**, which the packet lists as an escalation
     trigger. Adding the field to Stage A instead would bump the prompt/schema versions and invalidate
     all ~420 stored analyses, which CLAUDE.md says must not change casually;
   - extraction stays separate from the deterministic rule that uses it (invariant 1): the filter would
     be "role == principal", a deterministic downstream rule.
3. **Deterministic alternative:** design title rules for the two shapes and measure them on a hand-labelled
   gold set of a few hundred headlines (a human labelling task; I did not label a gold set). Smaller
   and cheaper, but each rule risks deleting real developments, and gold-set discipline applies.
4. **Do nothing and caveat:** keep the corpus as is and state that session sentiment is headline tone,
   not company-specific, in the methodology disclaimer (and in the result contract, §4.1). Consistent
   with the face-validity result: 6 of 12 clean, 4 mixed, 2 off-target.

### F2. Nominal false-detection level of the verdict rule

The spec states one nominal level: the **95% percentile-bootstrap interval for the mean** (§10), i.e.
5% two-sided for the "interval excludes zero" condition. It states no designed level for the complete
five-condition rule, and §15 forbids publishing an assumed rate. So the designed level is 5% for the
interval and "no more than that" for the full rule (the other conditions can only remove detections).

Measured on mean-zero noise (σ = 4.31%, 4,000 trials per cell, Monte-Carlo SE about 0.4 pp;
`scripts/mr003_nominal_level.py`):

| n | normal: interval alone / full rule | Student-t(3): interval alone / full rule |
|---|---|---|
| 20 | 7.0% / 6.8% | 8.1% / 8.0% |
| 30 | 6.6% / 6.5% | 7.2% / 7.0% |
| 50 | 5.5% / 5.4% | 6.8% / 6.5% |

**6–9% is not within the designed level.** At n = 20–30 the interval alone rejects at 1.3–1.6x its
nominal 5% (more than 4 Monte-Carlo SE), and heavy tails push it to 1.6x; only n = 50 with light tails
is within about 1 SE of 5%. The extra conditions barely help: when the interval excludes zero, split-half
sign agreement, mean/median agreement and the 0.5% floor almost always also hold, so they remove 0.3 pp
or less. The cause is the small-sample anticonservatism of the percentile bootstrap, not the other rules.
(An earlier 2,000-trial run gave 6.6% / 9.2% at n = 20; the difference is sampling noise.) In practice
the question is academic for NVDA and PFE, since no regime reaches n = 20 and the rule never reaches a
verdict; it matters once more history exists. No change was made to the procedure.

### F3. Day-0 values when no exact-timestamp event exists

**Confirmed for the aggregate; not for per-event fields.**
- Aggregate: when no resolved event is `exact`, `RegimeResult.path` contains no horizon-0 point
  (`point_at(0)` is `None`) and `day_zero_event_count` is 0. This is tested
  (`tests/test_market_reaction.py::test_lagged_events_are_excluded_from_day_zero_but_kept_for_one_through_ten`)
  and holds on the real data: NVDA and PFE negative paths start at horizon 1 with `n_day0 = 0`. When
  exact events exist, day 0 uses them only and carries its own `n`.
- Per event: `ReactionEvent.stock_returns[0]` and `market_adjusted_returns[0]` are populated for every
  event including `lagged` ones (observed on all sampled negative events). This is intended: the
  2026-09-19 amendment says full event provenance is preserved. But a consumer that renders per-event
  day-0 values (a drill-down, dots) would show a move that is probably the reaction to *earlier* news.
  The contract rule is therefore: **day-0 values may be shown only for events whose `timing_class` is
  `exact`, and never aggregated otherwise.** I have added this to the "must not infer" list (§4.1); no code was changed. If Alfred wants the contract to enforce rather than document it,
  masking day-0 on lagged events at projection time (MR-004) is the natural place.

### F4. Read-only scoping: how much history could be backfilled

No backfill was run and nothing was written. Evidence is the stored corpus, the watermarks, and the
backfill code (`backfill_historical_intelligence.py`, `backfill_service.py`, `sources/historical.py`).

**What the existing sources can do**
- **Google News RSS historical-range fallback** is the only source that has ever worked. It issues
  **one request per 30-day calendar bucket**; Google returns about 100 entries per query, which is why
  every bucket holds a steady 77–95 stored articles for NVDA and PFE. The configured per-bucket cap
  (`historical_news_max_articles`, default 180, max 500) is **not** the binding limit: the feed is.
  Raising it yields nothing. Splitting a bucket into shorter windows would multiply volume, but that is
  a code change that I did not make or test, and it would also change the sampling regime relative to
  the existing corpus.
- **GDELT** (the primary) has never succeeded: both tickers' watermarks show `ConnectTimeout`, zero rows.
  It cannot be counted on.
- **Reach:** NVDA/PFE history is exactly 12 months (the script default, `--months 12`; the horizon is
  an argument without a stated maximum). Yield per month does not decay across those 12 months, but
  nothing stored shows whether Google's date operators return anything older, so **depth beyond 12
  months is unknown** without a network test.
- **Corpus is a retrospective, relevance-ranked, date-capped sample** (MR-001 §2), with date-only
  stamps: more backfill gives more of the same quality, not better timestamps.
- **Current coverage:** AAPL and MSFT have only one month of historical-range backfill each (from
  2026-07-28 and 2026-07-22) plus live ingestion; AMZN has nothing. Only NVDA and PFE are in continuous
  coverage. Live ingestion since 2026-08-17 produces 3–4x the articles per month (NVDA 288 and 390 in
  Aug/Sep vs about 85–95 earlier).
- **Cost/risk of running it:** writes to the database, so it needs explicit approval; scoring is local
  FinBERT. The script calls the LLM only when `llm_api_key` is configured, otherwise it uses an
  "unavailable" provider, so a no-LLM run looks possible; I did **not** verify what, if anything, it
  records for analysis with the key unset. It must not run concurrently with a coverage cycle for the
  same ticker (script warning). Request volume is small (about one RSS request per ticker-month).

**Rough event yield** (extrapolated linearly from the only observed rates: 12 months of NVDA/PFE at the
provisional pair, i.e. negative 7 and 12, positive 15 and 15 resolved events; assumes the other names
yield about the same ~90 articles/month and the same tail rates; real rates will differ and thresholds
would be recomputed on a larger pool):

| horizon | negative events / company | positive events / company | any regime ≥ 20? |
|---|---|---|---|
| 12 months (reachable now) | about 7–12 | about 15 | no |
| 24 months | about 14–24 | about 30 | positive yes; negative borderline |
| 36 months | about 21–36 | about 45 | both, but needs depth not shown to exist |

- Positive reaches n ≥ 20 at about 16 months; negative at about 20–34 months.
- For **AAPL, MSFT, AMZN** a 12-month backfill gives them NVDA/PFE-like funnels: ≥ 63 qualified sessions
  (NVDA/PFE have 123–134), so history sufficiency would likely pass, but **events would be `preliminary`** (n
  about 7–15) exactly like NVDA/PFE. Nothing is gained toward a verdict from backfilling more names to 12
  months; verdict-capable regimes need depth (24+ months), which is the unknown part.
- Caveats: events are only independent once the +5 exclusivity is applied (already counted); the live era
  has so far contributed zero negative events because many-article sessions shrink toward zero; and no
  London-listed name has stored articles, so the London path would still be untested unless one is added.

### F5. What changed in the repository in this follow-up

Added `scripts/mr003_entity_relevance.py` and `scripts/mr003_nominal_level.py`; nothing under `src/`
changed; no new tests (the day-0 aggregate rule is already pinned). Database read-only, no network, no
LLM, no Git writes.

### F6. Decisions needed from Alfred (updated)

1. **Entity filter** (blocks the freeze, because it can move the thresholds and the events): choose
   among §F1.3 options 1 (role-extraction stage; needs a spec amendment and a schema decision), 3
   (deterministic title rules with a hand-labelled gold set), or 4 (no filter, caveat only).
   Recommendation: option 4 now for the freeze, because V1 shows the existing rule leaves thresholds and
   counts unchanged and the verdict tier is `preliminary` either way; revisit option 1 when more history
   makes a verdict reachable and the wrong-party noise starts to matter for it.
2. Backfill: 12 months for AAPL/MSFT/AMZN gives only `preliminary` evidence. A verdict needs 24+ months;
   decide whether to test Google's reach beyond 12 months (a network read, which I did not do) before
   deciding what to backfill.
3. Whether the day-0 rule should be enforced in the result projection (MR-004) rather than documented.
4. Whether to accept the measured 5.4–8% false-detection behaviour of the interval-based rule, or amend
   the interval method (a methodology change, not made).
5. The three earlier open items (§7) otherwise stand; pooling (A) / 0.42 is recorded as provisional.
