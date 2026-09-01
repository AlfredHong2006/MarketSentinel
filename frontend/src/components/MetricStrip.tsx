import type { CoverageView, MarketViewSummaryView } from "../api/types";
import { formatDate } from "../format";

/**
 * Four independently-computed observations (dashboard_market_view.py). Rendered verbatim,
 * side by side, and never fused into one score or verdict — that separation is a stated
 * architectural invariant, not a layout choice.
 */
export function MetricStrip({
  marketView,
  coverage,
}: {
  marketView: MarketViewSummaryView;
  coverage: CoverageView;
}) {
  const tiles: { label: string; note: string }[] = [
    { label: "Price", note: marketView.price_note },
    { label: "Sentiment", note: marketView.sentiment_note },
    { label: "Risk", note: marketView.risk_note },
    { label: "Intelligence", note: marketView.intelligence_note },
  ];

  return (
    <div className="ms-metric-strip">
      {tiles.map((tile) => (
        <div className="ms-metric-tile" key={tile.label}>
          <div className="ms-eyebrow">{tile.label}</div>
          <p className="ms-metric-note">{tile.note}</p>
        </div>
      ))}
      <div className="ms-metric-tile">
        <div className="ms-eyebrow">Recent coverage · {coverage.window_days}d</div>
        <p className="ms-metric-note">
          {coverage.articles} articles read · {coverage.analysed_articles} analysed
          {coverage.latest_sentiment && (
            <> · latest sentiment {formatDate(coverage.latest_sentiment.date)}</>
          )}
        </p>
      </div>
    </div>
  );
}
