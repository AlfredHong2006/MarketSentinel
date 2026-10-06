# MarketSentinel Decisions

Append-only record of consequential product, methodology and architecture decisions.

Keep entries concise. Ordinary implementation choices do not belong here.

---

## 2026-09-15 — Historical Market Reaction replaces “strongest sentiment correlation”

**Decision:** Build `mr-v1` as a conditional historical market-reaction/event-study layer rather than selecting the strongest sentiment/forward-return correlation across many horizons.

**Primary framing:** clearly positive/negative news sessions -> benchmark-adjusted historical market reaction.

**Primary horizon:** fixed +5 trading sessions. Display day 0 through +10 descriptively. Day 0 is contemporaneous, not predictive.

**Reason:** avoids horizon cherry-picking, matches the user's conditional question, supports honest null results, and creates reusable event/session primitives for later Event-Type Impact and Historical Analogues.

**Important constraints:** leakage-safe session alignment, adjusted prices, market benchmarks, uncertainty, no causal/predictive language, no paid LLM requirement for the core historical analysis.

**Amendments to lead design:**
- mean/median sign disagreement cannot yield `detected`;
- extreme returns are flagged/validated, not automatically removed by a numeric cutoff;
- null copy is “No consistent subsequent move detected,” not “priced in by the close”;
- placebo false-positive behavior must be measured, not assumed;
- numeric `tau` becomes immutable once frozen for `mr-v1`.

---

## 2026-09-15 — Lightweight multi-agent operating model

**Decision:** Move MarketSentinel from serial chat-driven implementation to durable repo work packets and bounded parallel agents.

**Operating model:**
- Alfred owns product direction, consequential decisions, integration and deploys;
- strongest Claude/Fable is reserved for difficult methodology/architecture gates;
- maximum two simultaneous MarketSentinel implementation agents initially;
- isolated Git worktrees per active workstream;
- Wave 1: MR-001 data readiness + MR-002 quant core in parallel;
- MR-003 real-data validation is sequential and freezes the result contract;
- after validation, MR-004 API/snapshot + MR-005 frontend may run in parallel;
- prefer one scheduled integration window per day over interrupt-driven supervision.

**Git policy:** retain the existing safety rule for now: agents do not commit/push/merge/rebase/reset; Alfred performs Git writes. Reconsider branch-local agent commits only if this becomes a measured throughput bottleneck.

**Reason:** obtain most of the wall-clock speedup of multi-agent development without turning a solo-founder project into a high-overhead pseudo-enterprise process.

---

## 2026-09-19 — `mr-v1` principal methodology review: four amendments

**Context:** principal review of `mr-v1` after MR-001 data readiness and the MR-002 quant core.
All four amendments are approved and binding on `docs/product/HISTORICAL_MARKET_REACTION_V1.md`.
Numeric thresholds are still **not** frozen; MR-003 freezes them.

**1. Asymmetric regime thresholds replace a single symmetric `tau`.**

The interface is now a `RegimeThresholds(negative, positive)` pair. Selection population `E` is the
pooled session signals with `distinct_sources >= 3`.

```text
tau_positive = max(0.20, round(Q0.85(S | E), 2))
tau_negative = max(0.20, round(-Q0.15(S | E), 2))
positive event iff S_t >=  tau_positive
negative event iff S_t <= -tau_negative
```

Quantiles are linear-interpolated; selection consumes sentiment only and never returns. The result
records both thresholds, both observed tail shares, and provisional/frozen state. The floor applies
to each tail independently, and **neither threshold may be loosened to recover event counts.**

*Reason:* company-news sentiment is skewed, so one symmetric threshold silently makes one regime a
far rarer and more extreme event class than the other. Measured on the MR-001 NVDA/PFE fixture, the
procedure gives `tau_positive = 0.44` / `tau_negative = 0.22` with both tails at 15.1%, where the
old symmetric rule gave 23.4% / 6.5%.

**2. Day 0 uses exact-timing events only.**

Session assignment is unchanged. Each session signal and event now carries `date_only_share` and
`timing_class` (`exact` when `date_only_share == 0`, else `lagged`). The day-0 aggregate excludes
every lagged event and exposes its own `n_day0`; horizons +1..+10 and the primary +5 statistic keep
all resolved events. Because the cohorts differ, every path horizon states its own cohort and `n`
explicitly rather than letting a consumer assume they match. Full event provenance is preserved.

*Reason:* the conservative date-only rule shifts an article one session late, so a lagged event's
day-0 move is not the reaction to that news. It remains valid for forward horizons, whose anchor
close is after publication either way.

**3. History sufficiency is three conditions, not one span.**

```text
span     = last_signal_index - first_signal_index  >= 126
density  = N_signal / (span + 1)                   >= 0.50
coverage = N_eligible                              >= 63     (sessions with >=3 distinct sources)
```

All three are required and all three observed values are reported.

*Reason:* span alone admits two dense clusters separated by a year of silence. The MR-001 real
fixture is exactly that shape — span 219 with 40 signal sessions — and correctly becomes
`not_enough_history` under the amended rule. The fixture itself is not relabelled.

**4. Benchmarks are investable trackers, not price indices.**

US -> `SPY`; London -> `CUKX.L`. `^GSPC` / `^FTSE` are retained only as validation/reference inputs
where already present.

*Reason:* a price index drops the benchmark's dividend yield while the stock leg is
dividend-adjusted, biasing every market-adjusted return upward by roughly that yield. A total-return
tracker makes the subtraction like for like.
