# MR-001 — Historical Market Reaction Data Readiness

Status: READY  
Owner: Claude Code A  
Depends on: none  
Worktree: `C:\Dev\MS-worktrees\data`

## Objective

Determine whether the real MarketSentinel data can support `mr-v1` honestly, and produce a small reusable real-data fixture for the quant worker as early as possible.

This is primarily an inspection/data-readiness workstream, not a product-feature implementation.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md` if present
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`

The approved methodology is not yours to redesign.

## Required investigation

For NVDA, PFE, AAPL, MSFT and AMZN, report:

1. stored historical article span;
2. total real sentiment-scored articles;
3. approximate number of trading sessions with eligible news;
4. publication timestamp quality:
   - full timestamp;
   - date-only / ambiguous;
   - unusable;
5. stock-price history available;
6. whether the current price field is genuinely split/dividend-adjusted;
7. whether existing company metadata identifies US vs London listing/calendar;
8. whether an S&P 500 and FTSE 100 benchmark series already exists;
9. whether benchmark data can reuse the current market-data path;
10. approximate session-sentiment distribution and plausible event counts under the `mr-v1` threshold-selection procedure;
11. how many current names appear capable of:
    - >=126 signal-history sessions;
    - >=10 events in a regime;
    - >=20 events in a regime;
12. material data-lineage risks that could make the feature misleading.

Do not inspect returns while choosing/tuning a candidate `tau`.

## Early handoff requirement

As soon as feasible, produce a **small deterministic real fixture** for MR-002 containing representative:
- article timestamps/sentiment;
- article IDs/titles/sources;
- real trading dates;
- stock prices;
- benchmark prices if available.

Prefer a few hundred rows from NVDA/PFE or another representative covered name.

The fixture must contain no secrets and must be suitable for offline tests.

Do not wait for the final report before producing this fixture if it can safely be created earlier.

## Allowed scope

May add:
- a readiness/report document under `docs/research/` or equivalent;
- offline inspection scripts under `scripts/` if needed;
- sanitized deterministic test fixtures under `tests/fixtures/market_reaction/` or the repo's equivalent fixture location;
- narrowly scoped tests for data assumptions.

Avoid changing production coverage behavior.

## Do not touch

- frontend;
- public-request system;
- coverage budgets/fairness;
- LLM prompts/contracts;
- public/private R2 security model;
- market-reaction production engine owned by MR-002.

## Acceptance criteria

- [ ] findings for all five target tickers are quantified, not guessed;
- [ ] adjusted-price semantics are verified from code/data lineage;
- [ ] exchange-calendar metadata readiness is established;
- [ ] benchmark availability/path is established;
- [ ] timestamp-quality percentages are reported;
- [ ] current-history/event eligibility is estimated;
- [ ] a sanitized real fixture is available for MR-002, or a clear reason explains why not;
- [ ] no paid LLM calls;
- [ ] no destructive/network mutation;
- [ ] any added tests/checks pass.

## Escalate only if

Stop feature progress only if a discovered issue makes the approved methodology fundamentally misleading or impossible with the current data.

Otherwise report gaps and recommend the smallest enabling change without redesigning `mr-v1`.

## Git rules

No push, merge, rebase, reset, or main modification.  
Current policy: no commits; Alfred performs Git writes.

## Completion report

Return:

1. **Readiness verdict:** READY / READY WITH GAPS / BLOCKED
2. table for NVDA/PFE/AAPL/MSFT/AMZN
3. timestamp findings
4. price/benchmark findings
5. exchange-calendar findings
6. event/history eligibility estimates
7. fixture produced
8. smallest required enabling changes
9. tests/checks run
10. assumptions still unvalidated
