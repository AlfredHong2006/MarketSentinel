import type { KeyDevelopmentsView } from "../api/types";
import { directionLabel, formatDate, horizonLabel } from "../format";
import { Pane } from "./Pane";
import { EvidenceStrength } from "./controls";

function directionTone(direction: string): "positive" | "negative" | "neutral" {
  if (direction === "positive") return "positive";
  if (direction === "negative") return "negative";
  return "neutral";
}

export function DevelopmentsPane({
  keyDevelopments,
  markerOrdinalByArticleId,
  selectedArticleId,
  onSelect,
}: {
  keyDevelopments: KeyDevelopmentsView;
  markerOrdinalByArticleId: Map<string, number>;
  selectedArticleId: string | null;
  onSelect: (articleId: string) => void;
}) {
  return (
    <Pane
      title="Key developments"
      meta={keyDevelopments.caption}
      controls={<span className="ms-pane-hint">Sorted by materiality</span>}
      className="ms-developments-pane"
    >
      {keyDevelopments.rows.length === 0 ? (
        <p className="ms-empty-note">{keyDevelopments.empty_message}</p>
      ) : (
        <ul className="ms-development-list">
          {keyDevelopments.rows.map((row) => {
            const ordinal = markerOrdinalByArticleId.get(row.article_id) ?? null;
            const isSelected = row.article_id === selectedArticleId;
            return (
              <li key={row.article_id}>
                <article
                  tabIndex={0}
                  className={`ms-development-row${isSelected ? " ms-row-selected" : ""}`}
                  onClick={() => onSelect(row.article_id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(row.article_id);
                    }
                  }}
                >
                  <div className="ms-development-main">
                    <div className="ms-development-meta-row">
                      <span className={`ms-tag ms-tag-${directionTone(row.event.event.direction)}`}>
                        {directionLabel(row.event.event.direction)}
                      </span>
                      <span className="ms-num-qualifier">
                        {formatDate(row.event.source_reference.published_at)}
                      </span>
                      <span className="ms-qualifier">· {row.provenance_note}</span>
                    </div>
                    <h3 className="ms-development-title">{row.event.event.summary}</h3>
                    <a
                      className="ms-development-source"
                      href={row.event.source_reference.url}
                      target="_blank"
                      rel="noreferrer"
                      onClick={(event) => event.stopPropagation()}
                    >
                      {row.event.source_reference.title}
                    </a>
                    <div className="ms-development-footer">
                      <span className="ms-qualifier">
                        {row.impact_label} · {row.tier_label} · {horizonLabel(row.event.event.time_horizon)}{" "}
                        horizon · {row.primary_source_label}
                      </span>
                      <EvidenceStrength
                        level={row.event.evidence_strength}
                        label={row.corroboration.summary_label}
                      />
                    </div>
                    {row.corroboration.contradiction_label && (
                      <div className="ms-contradiction">{row.corroboration.contradiction_label}</div>
                    )}
                  </div>
                  <div className="ms-development-marker">
                    <span className="ms-num-qualifier">
                      {ordinal !== null ? `marker ${ordinal}` : "—"}
                    </span>
                  </div>
                </article>
              </li>
            );
          })}
        </ul>
      )}
    </Pane>
  );
}
