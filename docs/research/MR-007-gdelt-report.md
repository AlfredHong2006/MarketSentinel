# MR-007 — Can MarketSentinel obtain 24+ months of company-news history?

Probed 2026-10-07 from Alfred's machine. Probe: `scripts/mr007_probe_history.py` (read-only; the
only file it writes is an optional `--out` JSON in the session scratchpad). Stored corpus read
read-only (`mode=ro`) for overlap only. No database write, no LLM, no `.env`, no return or
outcome inspected, no Git write.

## Verdict: 24+ months OBTAINABLE WITH LIMITS

- **Source: Google News RSS date-bounded search, not GDELT.** It returned a full page (100
  entries) for 30-day windows centred 13, 18, 24 and 36 months back, for NVDA and PFE, with no
  yield decay.
- **Limits:** (1) 92–98% of in-window entries are date-only (Pacific midnight), the same as the
  stored corpus, so every event stays `lagged` and day 0 stays empty; (2) it is a capped,
  relevance-ranked sample of what Google still surfaces, not an archive; (3) only NVDA and PFE
  were probed, and only for Google; (4) it writes to the database, so it is Alfred's decision.
- **GDELT DOC 2.0 could not be evaluated**: it returned HTTP 429 on the first request, twice.
  Reach, volume, timestamp quality, diversity and relevance for the DOC API are therefore
  **not measured**, not "bad".
- **Bulk GDELT files** are reachable and have URL, title and real publication times for about half
  of rows, but cost roughly 170 GB of download for 24 months of all-news. Not recommended for this
  need now.

## Network requests made: 16 HTTP GETs (plus connection checks and doc reads)

| endpoint | requests | result |
|---|---|---|
| `api.gdeltproject.org` DOC 2.0 | 2 | both HTTP 429 on the first request of the run (run stopped each time) |
| `news.google.com/rss/search` | 13 | all HTTP 200 (8 `google-windows`, 5 `google-shorter`) |
| `data.gdeltproject.org` GKG file | 1 | HTTP 200, one 15-minute file, 2.4 MB |

Also made, not counted above: 2 DNS lookups and 2 TCP+TLS handshakes to `api.gdeltproject.org`
(no payload sent), and 3 public documentation pages read through the web-fetch tool
(`gdeltproject.org/data.html`, the GKG 2.1 codebook PDF, the GDELT 2.0 announcement blog).
`data.gdeltproject.org` is a third host beyond the two named in the packet; Alfred approved the one
sample file explicitly. No request went to any other host. Pacing was 5.25 s between requests
within a run.

## 1. Why GDELT fails

| check | result |
|---|---|
| DNS | ok, 104.197.47.124 |
| TCP connect | ok, 0.12 s |
| TLS 1.3 handshake | ok, **9.17 s** (second run; the first run's timings were lost to a script bug, fixed since) |
| exact provider request (NVDA, 5-day window, `maxrecords=250`, 10 s timeout) | **HTTP 429**, body: "Please limit requests to one every 5 seconds or contact … All high-traffic users should switch to our ngrams dataset" |
| same, 30 s timeout / minimal query `nvidia` | not sent (run stopped on the 429, as required) |

- **The request shape is not the cause of the 429.** It came on the first request of each run,
  twice, more than 10 minutes apart, so request pacing from this script is not the trigger. The
  address (or a shared egress address) is being throttled, or the DOC API sheds load. I cannot
  tell which from two samples.
- **`ConnectTimeout` in the stored watermarks is probably the 10 s timeout against a slow TLS
  handshake.** httpx applies the 10 s connect timeout to TCP plus TLS; one measured handshake took
  9.17 s. That is one sample and a hypothesis, not a proof. It was not reproduced as a timeout here
  (the handshake finished just inside 10 s).
- **Ruled out:** DNS failure, TCP failure, TLS failure, malformed query (the server parsed it and
  answered), a block page. **Not separated:** whether 429 or timeout is what Actions sees.
- **No code change.** A longer timeout cannot be validated while the endpoint answers 429, so it is
  a recommendation, not a bug fix: raise the connect timeout, and treat 429 as "provider
  unavailable" for the run (the provider already does not retry it).
- **GitHub Actions:** may differ in both directions (a different address, so possibly no 429;
  possibly the same slow route). One-line check in a later workflow step, no storage:
  `curl -s -o /dev/null -w "%{time_connect} %{time_appconnect} %{http_code}\n" "https://api.gdeltproject.org/api/v2/doc/doc?query=nvidia&mode=artlist&maxrecords=1&format=json"`.

## 2. GDELT DOC 2.0 content (reach, volume, timestamps, diversity, relevance, overlap)

**Not measured.** The windows run (`gdelt-windows`: NVDA, PFE, AZN at 3/6/12/24/36 months, 15
requests) was not sent because the endpoint returned 429 on every first request. The mode exists
in the script. For orientation only, from the provider code and public docs, not measured here:
`maxrecords` caps at 250 per request; `seendate` is observation time with seconds; the repo's
5-day windows over 24 months would be about 146 requests, roughly 13 minutes at 5.25 s.

### 2b. GDELT bulk files (assessed, one file fetched)

Documented: GKG 2.x and Events 2.x files every 15 minutes from 2015-02-19, listed in
`data.gdeltproject.org/gdeltv2/masterfilelist.txt`; named `YYYYMMDDHHMMSS.gkg.csv.zip`; free; the
pages I read state no download rate limit or per-day size.

Measured on the single file `20230115120000.gkg.csv.zip` (a Sunday-noon slot about 33 months old,
which also shows files that old still exist):

| item | result |
|---|---|
| size | 2.41 MB zipped, 7.55 MB unzipped, 583 rows, 27 tab-separated columns on every row |
| article URL | 583 / 583 |
| article title | 583 / 583 (inside the last column as `<PAGE_TITLE>`) |
| publisher timestamp | 305 / 583 (52%) carry `PAGE_PRECISEPUBTIMESTAMP`; otherwise only GDELT's 15-minute ingest slot |
| distinct domains | 218 |
| organisation names | **not measured**: my script read the themes column by mistake; the file was not refetched (one-file limit). The GKG 2.1 layout has persons, organisations and enhanced organisations columns; whether they match our tickers is unvalidated |
| company-name mentions | **not measured**, same bug |

Scaling the one slot (assumption: Sunday noon is typical; weekday volume is likely higher):
about 230 MB zipped and 720 MB unzipped per day, so about 170 GB zipped for 24 months of every
news article, only a tiny share of which concerns the tickers. Events files carry a source URL but
no title. A filtered pipeline (download, keep matching rows, discard) is possible but is new
ingestion code and a storage design, outside this packet. BigQuery would avoid the download but
needs an account; not called, only described.

## 3. Google News RSS reach (measured)

30-day windows ending N months ago, query = the provider's own `_google_historical_query`.

| ticker | age | returned | in window | relevant (≥0.5) | date-only (Pacific midnight) | with time of day | days covered | days with ≥3 publishers | max publishers/day |
|---|---|---|---|---|---|---|---|---|---|
| NVDA | 13 m | 100 | 97 | 87 | 97 | 0 | 19 | 10 | 10 |
| NVDA | 18 m | 100 | 93 | 82 | 93 | 0 | 21 | 9 | 11 |
| NVDA | 24 m | 100 | 96 | 84 | 92 | 4 | 25 | 13 | 5 |
| NVDA | 36 m | 100 | 92 | 78 | 92 | 0 | 23 | 10 | 7 |
| PFE | 13 m | 100 | 96 | 72 | 91 | 5 | 22 | 11 | 10 |
| PFE | 18 m | 100 | 98 | 81 | 93 | 5 | 20 | 10 | 15 |
| PFE | 24 m | 100 | 100 | 87 | 98 | 2 | 19 | 10 | 12 |
| PFE | 36 m | 100 | 94 | 76 | 92 | 2 | 22 | 13 | 7 |

- **Reach:** to 36 months at least, for both tickers. Google was not asked for anything older.
- **Decay:** none visible. But every page hits the 100-entry cap, so this shows the cap, not how
  many articles exist. It matches the stored 12 months (77–95 stored per bucket).
- **Timestamps:** 91–98% of in-window entries are 00:00 America/Los_Angeles, the same as the
  stored corpus (86%). The stamp is a date, not a time, and does not improve with depth.
- **Source diversity:** about half the covered days have 3+ publishers in the window, similar to
  the stored 57% of sessions with ≥3 sources. Developer-blog and issuer pages (NVIDIA Developer,
  NVIDIA Blog) are the top "publisher" for NVDA; they count as one issuer voice, as today.
- **Shorter windows** (NVDA, 24 months back, one 30-day window against four 7-day windows):

| window | returned | in window | relevant | distinct titles | days covered |
|---|---|---|---|---|---|
| 30 d | 100 | 96 | 84 | 84 | 26 |
| 7 d #1 | 100 | 70 | 61 | 61 | 7 |
| 7 d #2 | 100 | 70 | 61 | 60 | 7 |
| 7 d #3 | 100 | 74 | 62 | 61 | 7 |
| 7 d #4 | 100 | 57 | 52 | 52 | 7 |

  Four weekly windows give 236 relevant rows against 84, about 2.8x (duplicates between windows
  not removed), and cover every day. They also leak outside their window (26–43% of entries fall
  outside the requested week), so Google's date operators are loose at week scale. The 30-day
  buckets the backfill already uses are the sampling regime of the stored corpus; shorter windows
  would change it.
- **Overlap with stored corpus:** only NVDA 13 months overlaps the stored span (it starts
  2025-08-27). There 90 relevant probe rows met 79 stored rows in the window, with 29 matching by
  normalized title and 30 by URL. At 18 months and older nothing is stored, so there is nothing to
  overlap. The GDELT overlap was not measured.

## 4. Mixing sources or regimes (risk only; no methodology change proposed)

MR-001 §7.3 already shows the mechanism. Applied here:

- **Same source, deeper months:** Google 13–36 months looks like the stored 12 months (same cap,
  same date-only share, same per-month yield), so a 24-month Google-only corpus is one regime, and
  the pooled distribution should behave like today's.
- **Adding a GDELT era:** a source with real times and many more articles per day would raise the
  articles per session and the distinct-source count, pull session sentiment toward zero (means
  over more articles shrink: |S| ≥ 0.2 in 67% of 3–5-article sessions against 46% of 11+-article
  sessions), and so change tail membership by coverage depth rather than by news. Pooled `tau`
  would be driven by the dense era, thin Google sessions would fall out of the tails, and a
  chronological split-half would compare sources.
- **Dense versus thin periods inside Google:** the shorter-window result shows the same company
  and month can yield 84 or 236 rows depending on window width; mixing widths across months would
  create the same artefact.
- **Live era:** already known to give 3–4x articles per month after 2026-08-17.

## 5. Cost of 24 months from Google News RSS (recommended option)

Assumptions: the existing 30-day bucket backfill; per-bucket yield as stored (about 85 relevant
articles per bucket, matching the probe's 72–90); the MR-003 observed rates; the stored 12 months
as the first year.

| item | estimate |
|---|---|
| requests | 12 RSS requests per ticker for months 13–24 (24 per ticker for a full 24-month run); about 63 s per ticker at 5.25 s pacing. The current Google provider does not pace itself, and resolving up to 10 Google redirects per bucket adds requests to publisher hosts, which this probe did not test |
| articles | about 1,000 extra per ticker, about 2,000 for NVDA + PFE |
| signal sessions | about 240 extra per ticker; about 135 of them with ≥3 sources (MR-001: 138 / 134 on 12 months) |
| events, extrapolated linearly from MR-003 (12 m: NVDA neg 7 / pos 15, PFE neg 12 / pos 15) | 24 m: NVDA ~14 / ~30, PFE ~24 / ~30; 36 m: NVDA ~21 / ~45, PFE ~36 / ~45 |
| first regimes to reach n ≥ 20 | positive at about 16 months; negative PFE at about 20, NVDA at about 34 months |
| role labelling (`gpt-4o-mini`, $0.15 / $0.60 per 1M tokens) | cost = N × (T_in × 0.15 + T_out × 0.60) / 1,000,000. With T_in ≈ 350 and T_out ≈ 30 per label (assumed from MR-003's ~1M tokens for 2,835 labels), about $0.00007 per label: ≈ $0.14 for 2,000 articles, ≈ $0.20 for the 2,852 already planned |

Verdict-capable depth (n ≥ 20) at 24 months: **NVDA positive, PFE positive and PFE negative
(borderline); NVDA negative is not reached until about 34 months, if Google's 36-month reach holds
for 36 more months.** These are linear extrapolations; the role filter (decided 2026-10-07) will
shrink every count by an unmeasured share, so the real numbers will be lower. AAPL, MSFT, AMZN
and the London names are not brought to depth by anything probed here.

## 6. Recommendation: DOC API, bulk files, or neither

- **DOC API:** not usable from this address today (429 on the first request, twice). Not
  recommended as a backfill route until a Actions-side check (§1) shows it answers.
- **Bulk GKG files:** workable and richer (URL, title, real publication times for about half the
  rows), but about 170 GB for 24 months of all-news and a new filter-and-store pipeline, for two
  tickers. Not recommended now. It is the only route that could give a real time of day; keep it
  as an option if date-only stamps become the blocker rather than history depth.
- **Neither, for now:** extend Google News RSS to 24 months (months 13–24) for NVDA and PFE.

## Decisions needed from Alfred

1. Whether to run a 24-month Google backfill for NVDA and PFE (database write, and the role-label
   spend on the new articles). `--months` sets a horizon only, so a plain `--months 24` run would
   re-plan all 24 calendar buckets, including the 12 already stored; fetching only months 13–24
   needs a decision on how.
2. Whether date-only stamps (92–98%, every event `lagged`) are acceptable for verdicts, or the
   bulk-file route is worth scoping.
3. Whether to run the one-line Actions curl check (§1) to settle GDELT DOC before dropping it.

## Assumptions still unvalidated

- GDELT DOC content: reach, volume, timestamps, diversity, relevance, overlap, the London name.
- Whether the 429 is address-based and whether Actions sees it; whether the slow TLS handshake
  explains the stored `ConnectTimeout`.
- GKG organisation matching to tickers; bulk-file volume on a weekday; any download rate limit.
- Google for AAPL, MSFT, AMZN, London names; Google beyond 36 months.
- Whether Google's 100-entry cap hides most of each month (shorter windows suggest it does).
- Event yield at 24–36 months (linear extrapolation; the role filter will lower it).
- The redirect-resolution requests of the existing Google provider (publisher hosts).
