# Design provenance

The approved visual reference for this frontend slice is:

```
C:\dev\MarketSentinel-design\03_Design chat title_____MarketSentinel Company Overview__\
```

Specifically:

- `CompanyOverview.dc.html` — the approved Company Overview layout (design canvas
  component: dark 40px app header, 196px left rail, identity strip + metric row, a
  developments/risks split pane, a chart pane, and a persistent 392px detail region, plus a
  24px bottom utility strip).
- `_ds/marketsentinel-design-system-66d55e7d-.../readme.md` — the design system brief:
  visual thesis, content/voice rules, colour/typography/spacing philosophy, component
  inventory, and the "what is rejected" list (no cards, no shadows except overlays, one
  reserved accent, near-zero radius, hueless evidence strength, warm-paper substrate).
- `_ds/marketsentinel-design-system-66d55e7d-.../_ds_manifest.json` — the resolved token
  values (colors, typography, spacing, shape, motion) used to vendor `src/ds/`.

## What was vendored verbatim

`src/ds/tokens/*.css` and `src/ds/styles.css` are byte-for-byte copies of the design
system's own token files (fonts, colors, typography, spacing, shape, motion, base), taken
from the `_ds/marketsentinel-design-system-.../tokens/` folder. No token value was
redefined, renamed, or reinterpreted; every colour, spacing, radius, and type value the
app uses comes from these files.

## What was reimplemented, and why

The design system's actual components (`Button`, `Pane`, `DevelopmentRow`, `RiskMarker`,
`EvidenceStrength`, `TimeSeriesChart`, `TimeRange`, `SidebarNav`, `DetailPane`, etc.) exist
only as `_ds_bundle.js` — a compiled bundle for the proprietary design-canvas runtime
(`DCLogic`, `x-import`, `sc-for`/`sc-if` custom elements, driven by `support.js`). That
runtime is not a portable React/ES-module library and cannot be imported into a standalone
Vite app, so no component source (`.jsx`) or usage guide (`.prompt.md`) exists on disk to
vendor for this slice.

Instead, `src/components/*.tsx` reimplements the same visual components directly against the
vendored tokens, matching `CompanyOverview.dc.html`'s structure, spacing, and typography
rules class-for-class (32px pane headers, 1px hairline gutters, one reserved accent on
selection/focus, hueless evidence strength, no cards/shadows/gradients, sentence case,
middot-separated qualifier strings, `28 Aug 2026`-style dates).

## Deliberate departures from the reference mock

The reference `.dc.html` uses placeholder/mock data shaped for a generic "Placeholder
Industrials Co." and a few concepts that do not exist in the live `CompanyOverview` API
contract (`src/marketsentinel/domain.py`). Per this task's constraints (no invented data, no
reimplemented business logic), the following were adapted rather than copied:

- **No "Composite risk" hero score.** The mock shows one blended 0–100 risk number. The API
  and `ARCHITECTURE.md` explicitly forbid fusing the four market-view observations or the
  per-theme risk scores into one composite ("dashboard_market_view... never fuses them into
  an overall score, verdict, or stance"). The metric strip instead renders the four
  independently-computed `market_view` notes (price/sentiment/risk/intelligence) verbatim.
- **No separate "why it matters" sentence.** The mock's development rows show a title and a
  distinct one-sentence rationale. The API's `EventExtraction` has one `summary` field, not
  two; the title-equivalent role is filled by `event.summary` and no second sentence is
  fabricated.
- **No peer-median chart series.** The mock chart plots the subject against a peer index.
  There is no peer/benchmark series in the API — the chart renders only the subject's
  observed price.
- **No exchange/sector/market-open-closed badge.** `Constituent` carries `symbol`, `name`,
  and `market` (index membership) only; no exchange, sector, or live market-state field
  exists, so the identity strip omits them rather than inventing values.
- **"Track company" / "Export brief" / "Pin to thesis" render disabled**, each with an
  honest `title` tooltip, mirroring the mock's own already-disabled "Pin to thesis · not yet
  supported" pattern — there is no backing capability for any of the three in this
  read-only slice.
- **Sidebar rail carries company search, not a one-item nav list.** The mock's rail lists
  "Overview" / "Risk register" / "Sources" as destinations; only Overview exists, so a
  single-item nav had no genuine navigation value. It was replaced with the constituent
  search the Streamlit sidebar used (`GET /api/v1/constituents/search`) — a real,
  functional destination-picker rather than dead chrome.
- **Chart library: Recharts (v3).** The Company Overview chart was reimplemented with
  Recharts — a mature, actively maintained React charting library — to reproduce the
  Streamlit/Plotly chart's actual behaviour (dual-axis price + sentiment overlay, hover
  tooltips, clickable event markers) rather than the hand-rolled SVG from the first slice.
  Series/grid colours are the vendored design tokens (`var(--series-1)`, `var(--series-2)`,
  `var(--chart-grid)`, etc.), not Recharts' or Plotly's defaults, so the chart stays inside
  the approved warm-paper/hairline system rather than reverting to Streamlit's styling.
