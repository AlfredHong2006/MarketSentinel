# MarketSentinel Product

## Purpose

MarketSentinel helps an investor understand what is materially changing inside and around a public company without reading a large volume of repetitive financial news.

It is company intelligence for medium- and long-term research, not an intraday trading signal.

## The problem

Company news feeds are noisy:

- the same development is reported many times;
- important events are mixed with commentary and market reaction;
- sentiment alone does not explain what actually changed;
- company statements can look like corroboration when they are not;
- high-volume coverage can make one story appear more important than it is;
- persistent risks are difficult to infer from individual headlines.

A useful system should compress that noise into a small number of developments an investor can inspect and reason about.

## Differentiated core

The core MarketSentinel loop is:

financial news
→ structured company events
→ claim and evidence checking
→ materiality assessment
→ same-development grouping
→ ranked Key Developments
→ persistent Top Risks

The goal is not to maximise the number of events detected.

The goal is to surface the smallest useful set of genuinely material, evidence-grounded developments without losing important company information.

## What the user should be able to answer

After opening a company page, a user should quickly understand:

1. What materially changed?
2. Why might it matter to the business?
3. What evidence supports the development?
4. Are several reports describing the same underlying event?
5. How persistent is the development likely to be?
6. What downside themes are accumulating over time?
7. Which claims are uncertain, contradicted, or weakly corroborated?

## Product principles

### Intelligence quality over feature count

A smaller number of trustworthy developments is more useful than a large feed of superficially relevant articles.

### Evidence must remain honest

Company-controlled channels are useful primary evidence but do not become external corroboration merely because several company-owned publications repeat the claim.

Do not call sources "independent" unless independence is actually established.

### Events and materiality are different problems

LLM extraction describes what an article appears to say.

Materiality is a separate product decision about whether that event deserves investor attention.

The two should not be silently collapsed into one opaque model judgement.

### Duplicate reporting should not create duplicate importance

Multiple articles covering one underlying development should improve evidence breadth where appropriate, not create several separate high-ranking developments.

### Explainability matters

Important outputs should expose useful reasons: event type, direction, persistence, evidence/corroboration, materiality reasoning, and relevant transmission channels.

### Uncertainty should remain visible

Contradictory or incomplete evidence should be surfaced rather than hidden to make the interface appear more certain.

### Product judgement belongs to the product owner

Consequential decisions about scope, UX, priorities and what MarketSentinel should become are made by the product owner.

Implementation agents should not silently make those decisions.

## Supporting capabilities, not the current USP

These can support the core product but should not dominate development merely because they are technically interesting:

- generic financial-news aggregation;
- price charting;
- generic sentiment scoring;
- generic AI chat;
- visual animation/polish;
- forecasting or directional probabilities;
- commodity infrastructure that established libraries/services already solve well.

Forecasting should not drive product or investment-performance claims until it has appropriate validation and calibration.

## Feature priority test

Before adding substantial work, ask:

> Does this materially improve event recall, evidence quality, materiality precision, same-event consolidation/ranking, persistent risk intelligence, or investor comprehension?

If not, defer it unless there is a clear operational reason to do it.

## What excellent looks like

For a company such as Nvidia, MarketSentinel should let an investor move from:

> "There are hundreds of articles about this company."

to:

> "These are the few developments that materially changed the business picture, these reports describe the same underlying events, this is the evidence behind them, and these are the downside themes that remain persistent."

That is the product to optimise.
