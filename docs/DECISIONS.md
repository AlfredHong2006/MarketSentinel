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
