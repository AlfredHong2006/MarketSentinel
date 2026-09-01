import type { TopRisksView } from "../api/types";
import { formatDate } from "../format";
import { Pane } from "./Pane";
import { RiskMarker } from "./controls";

export function RisksPane({
  topRisks,
  selectedTheme,
  onSelect,
}: {
  topRisks: TopRisksView;
  selectedTheme: string | null;
  onSelect: (theme: string) => void;
}) {
  return (
    <Pane
      title="Top risks"
      meta={`ranked · ${topRisks.diagnostics.themes_ranked} total`}
      className="ms-risks-pane"
    >
      {topRisks.rows.length === 0 ? (
        <p className="ms-empty-note">{topRisks.empty_message}</p>
      ) : (
        <ul className="ms-risk-list">
          {topRisks.rows.map((row) => {
            const publisherCount = new Set([
              row.risk.primary_publisher,
              ...row.risk.supporting_publishers,
            ]).size;
            const isSelected = row.risk.theme === selectedTheme;
            return (
              <li key={row.risk.theme}>
                <div
                  tabIndex={0}
                  className={`ms-risk-row${isSelected ? " ms-row-selected" : ""}`}
                  onClick={() => onSelect(row.risk.theme)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(row.risk.theme);
                    }
                  }}
                >
                  <div className="ms-risk-row-title">
                    <span className="ms-num-qualifier">{row.rank}</span>
                    <h3 className="ms-risk-name">{row.label}</h3>
                  </div>
                  <div className="ms-risk-row-detail">
                    <RiskMarker value={row.concern_index} color={row.band_color} />
                    <span className="ms-qualifier">
                      {row.concern_index}/100 · band {row.band}
                    </span>
                  </div>
                  <div className="ms-risk-row-detail">
                    <span className="ms-qualifier">
                      {publisherCount} {publisherCount === 1 ? "publisher" : "publishers"} · updated{" "}
                      {formatDate(row.risk.latest_published_at)}
                    </span>
                  </div>
                  <p className="ms-risk-summary">{row.summary}</p>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <div className="ms-pane-footnote">{topRisks.caption}</div>
    </Pane>
  );
}
