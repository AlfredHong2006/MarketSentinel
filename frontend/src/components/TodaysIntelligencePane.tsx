import type { TodaysIntelligenceView } from "../api/types";
import { directionLabel, formatDate, horizonLabel } from "../format";
import { Pane } from "./Pane";
import { EvidenceStrength } from "./controls";

function directionTone(direction: string): "positive" | "negative" | "neutral" {
  if (direction === "positive") return "positive";
  if (direction === "negative") return "negative";
  return "neutral";
}

export function TodaysIntelligencePane({
  todaysIntelligence,
  selectedArticleId,
  onSelect,
}: {
  todaysIntelligence: TodaysIntelligenceView;
  selectedArticleId: string | null;
  onSelect: (articleId: string) => void;
}) {
  return (
    // "Top intelligence", not "Today's": prepare_todays_intelligence applies no date filter at
    // all -- it ranks every eligible stored analysis by magnitude, confidence, evidence strength,
    // source class and time, then takes the strongest few. Display wording only; the API field
    // and function names are unchanged.
    <Pane title="Top intelligence" meta={todaysIntelligence.caption} className="ms-intelligence-pane">
      {todaysIntelligence.cards.length === 0 ? (
        <p className="ms-empty-note">{todaysIntelligence.empty_message}</p>
      ) : (
        <ul className="ms-intelligence-list">
          {todaysIntelligence.cards.map((card) => {
            const isSelected = card.article_id === selectedArticleId;
            const event = card.event;
            return (
              <li key={card.article_id}>
                <article
                  tabIndex={0}
                  className={`ms-intelligence-card${isSelected ? " ms-row-selected" : ""}`}
                  onClick={() => onSelect(card.article_id)}
                  onKeyDown={(keyEvent) => {
                    if (keyEvent.key === "Enter" || keyEvent.key === " ") {
                      keyEvent.preventDefault();
                      onSelect(card.article_id);
                    }
                  }}
                >
                  <div className="ms-development-meta-row">
                    <span className={`ms-tag ms-tag-${directionTone(event.event.direction)}`}>
                      {directionLabel(event.event.direction)}
                    </span>
                    <span className="ms-num-qualifier">{formatDate(event.source_reference.published_at)}</span>
                  </div>
                  <h3 className="ms-intelligence-title">{event.event.summary}</h3>
                  <a
                    className="ms-development-source"
                    href={event.source_reference.url}
                    target="_blank"
                    rel="noreferrer"
                    onClick={(clickEvent) => clickEvent.stopPropagation()}
                  >
                    {event.source_reference.title}
                  </a>
                  <div className="ms-qualifier ms-intelligence-meta">
                    {card.impact_label} · {horizonLabel(event.event.time_horizon)} horizon ·{" "}
                    {card.primary_source_label}
                  </div>
                  <EvidenceStrength level={event.evidence_strength} label={card.corroboration.summary_label} />
                  {card.corroboration.contradiction_label && (
                    <div className="ms-contradiction">{card.corroboration.contradiction_label}</div>
                  )}
                </article>
              </li>
            );
          })}
        </ul>
      )}
    </Pane>
  );
}
