# MR-007 — GDELT Investigation for Deeper History

Status: DONE (merged to local main 2026-10-07; verdict OBTAINABLE WITH LIMITS)  
Owner: TBD  
Depends on: none  
Worktree: `C:\Dev\MS-worktrees\gdelt`

## Objective

Establish, with evidence, whether MarketSentinel can obtain 24 months or more of usable company-news
history, and from which source. MR-003 found that no regime on NVDA or PFE reaches the 20 events a
verdict needs on the 12 months stored today, and estimated that 24+ months would be required.

This is an investigation. It writes nothing to the database and backfills nothing. It ends with a
report and a recommendation for Alfred.

## Source of truth

Read:
- `CLAUDE.md`
- `AGENTS.md`
- `docs/DECISIONS.md`, the 2026-10-06 entry "Later market-reaction workstreams" and the 2026-09-13
  entries on continuous coverage
- `docs/product/HISTORICAL_MARKET_REACTION_V1.md`, section 4 (timestamps) and section 11 (history)
- `docs/research/MR-001-data-readiness-report.md`, sections 2 and 3
- `C:\Dev\MS-shared\reports\MR-003.md`, section F4
- `src/marketsentinel/sources/historical.py`, `historical_backfill.py`, `backfill_service.py`,
  `scripts/backfill_historical_intelligence.py`

Do not reinterpret or redesign approved product/methodology decisions.

## What is known

- GDELT DOC 2.0 is the configured primary historical source and has never returned a row. The
  stored watermarks show `ConnectTimeout` on every attempt. Those attempts ran from the scheduled
  GitHub Actions worker and from Alfred's machine; which of the two each failure came from has not
  been separated.
- Everything stored came from the Google News RSS historical-range fallback: one request per 30-day
  bucket, about 100 entries per request, mostly date-only timestamps.
- Whether Google's date operators return anything older than 12 months has never been tested.

## Permitted network use

This workstream needs network reads, and they are approved within these limits:

- read-only HTTP GET requests to the GDELT DOC 2.0 API and to Google News RSS search, the two
  endpoints the code already uses;
- small probes only: enough requests to answer the questions below, paced at least as slowly as the
  existing provider's pacing, and stopped immediately on rate-limit or block responses;
- any other endpoint or data service (GDELT via BigQuery, a paid news API, a web archive) may be
  **described from its documentation** but not called, signed up for, or paid for.

## Hard limits

- No database writes. Do not run `scripts/backfill_historical_intelligence.py`,
  `scripts/run_coverage_cycle.py`, or any other script that stores articles.
- No LLM or API-key use, no sentiment scoring of fetched articles into storage, no paid service.
- No real return or event outcome is computed or inspected.
- No Git writes.

## Questions to answer

### 1. Why does GDELT fail?

- Reproduce the failure from this machine with the existing provider and its exact request.
- Separate the causes: DNS, TCP connect, TLS, the 10-second timeout, request pacing, query shape,
  response size, or blocking by address. Try a longer timeout and a minimal query.
- State whether the failure is likely to differ on a GitHub Actions runner. Do not dispatch a
  workflow to find out; say what a later one-line check there would be.

### 2. If GDELT answers, what does it return?

- How far back does the DOC 2.0 article list reach for a company query? Test with dated windows at
  about 3, 6, 12, 24 and 36 months for NVDA and PFE, and one London-listed name.
- Articles per window, and the cap per request.
- **Timestamp quality.** `seendate` is when GDELT observed the article, not a publisher timestamp.
  Measure what share carry a real time of day, and whether they would be `exact` or date-only under
  the `mr-v1` timestamp rule. Today every resolved event is `lagged`; a source with real
  times of day would change what day 0 can show.
- Source diversity: distinct publishers per day, since `mr-v1` needs at least 3 per session.
- Relevance: the share of returned articles that pass the existing relevance and URL validation.
- Overlap with the stored Google corpus for the same windows, by normalized title or URL.

### 3. How far back does Google News RSS reach?

- Probe dated windows at 13, 18, 24 and 36 months for NVDA and PFE.
- Entries per window, and whether yield decays with age.
- Whether shorter windows return more articles in total, and what that would do to the sampling
  regime relative to the corpus already stored.

### 4. What would mixing sources or regimes do?

`mr-v1` pools sessions into one distribution to select thresholds. State what combining GDELT-era
and Google-era history, or dense and thin periods, would do to session sentiment, the source count
per session, and tail membership. MR-001 section 7 already documents this effect for the
live-versus-backfill break. Report it as a risk; do not propose a methodology change.

### 5. What would 24 months cost?

For the best option: requests, elapsed time at safe pacing, expected articles, expected sessions
with at least 3 sources, and a rough event yield per regime using MR-003's observed rates, with the
assumptions stated. Role labelling cost at the approved `gpt-4o-mini` price, as a formula on the
article count. Say which tickers this would bring to verdict-capable depth.

## Allowed scope

- a read-only probe script under `scripts/`;
- a small frozen sample of probe responses under `tests/fixtures/` only if a test needs it, with no
  secrets;
- `docs/research/MR-007-gdelt-report.md`;
- a code-level fix to the GDELT provider **only** if the failure is a plain bug (for example a
  malformed query or an unreasonable timeout), with a regression test that makes no network call.
  Anything that changes how much is fetched or stored is a recommendation, not a change.

## Do not touch

- the database, the coverage cycle, the ledger, the workflow;
- `docs/product/`, `docs/DECISIONS.md`, `docs/planning/`, other workstreams' packets;
- the `mr-v1` engine and its tests;
- frozen fixtures.

## Acceptance criteria

- [ ] the GDELT failure is reproduced and its cause is identified, or the report states exactly
      what was ruled out;
- [ ] reach, volume, timestamp quality, source diversity, relevance and overlap are measured for
      GDELT if it answers;
- [ ] Google News RSS reach beyond 12 months is measured;
- [ ] the report states plainly whether 24+ months is obtainable, from which source, for which
      tickers, and with what timestamp quality;
- [ ] the mixing-regimes risk is described;
- [ ] the cost of the recommended option is estimated with its assumptions;
- [ ] the number of network requests made is reported, and none went outside the two permitted
      endpoints;
- [ ] no database write, no backfill, no LLM call, no real outcome inspected;
- [ ] any code change has a network-free regression test; the full Python suite,
      `uv run ruff check .` and `uv run ruff format --check .` pass;
- [ ] no test makes a network call or reads the live database or `C:\Dev\MS-shared\`.

## Autonomy

Continue until all acceptance criteria pass.

Make ordinary implementation decisions independently. Do not stop for cosmetic choices.

A negative answer is a result. If neither source reaches 24 months, say so and stop; do not widen
the search to services this packet does not permit.

## Escalate only if

- a probe draws a block or a terms-of-use warning from either endpoint;
- answering a question would need a credential, a paid service, or a database write.

Record the issue in the report and continue all unblocked work.

## Git rules

Do not push, merge, rebase, reset, commit, stage, force checkout another worktree/branch, or modify
main. No Git writes of any kind.

## Completion report

Write `C:\Dev\MS-shared\reports\MR-007.md` from `docs/workstreams/REPORT_TEMPLATE.md`, and include:

1. **Verdict:** 24+ months OBTAINABLE / OBTAINABLE WITH LIMITS / NOT OBTAINABLE
2. the GDELT failure cause
3. the reach, volume and timestamp-quality table per source
4. the recommended backfill option and its cost, or the reason there is none
5. network requests made
6. decisions needed from Alfred — a backfill writes to the database and is his to approve
7. assumptions still unvalidated
