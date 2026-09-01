import type {
  AnalyzedEvent,
  CompanyIntelligenceEvent,
  CompanyOverview,
  CorroborationView,
  KeyDevelopmentView,
  RiskRowView,
  StoredArticleAnalysisView,
  TodaysIntelligenceCardView,
} from "../api/types";
import { directionLabel, evidenceStatusLabel, eventTypeLabel, formatDate, horizonLabel } from "../format";
import type { Selection } from "../selection";
import { RiskMarker, SourceRef } from "./controls";

/** The lazily-fetched stored analysis behind a selected Relevant News row. */
export type ArticleAnalysisState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; analysis: StoredArticleAnalysisView };

export function DetailPane({
  overview,
  selection,
  markerOrdinalByArticleId,
  articleAnalysis,
}: {
  overview: CompanyOverview;
  selection: Selection | null;
  markerOrdinalByArticleId: Map<string, number>;
  articleAnalysis: ArticleAnalysisState | null;
}) {
  if (selection?.kind === "article") {
    return (
      <ArticleDetail
        state={articleAnalysis}
        ordinal={markerOrdinalByArticleId.get(selection.articleId) ?? null}
      />
    );
  }
  if (selection?.kind === "development") {
    const row = overview.key_developments.rows.find((r) => r.article_id === selection.articleId);
    if (row) {
      return <DevelopmentDetail row={row} ordinal={markerOrdinalByArticleId.get(row.article_id) ?? null} />;
    }
    const marker = findMarker(overview, selection.articleId);
    if (marker) {
      return <MarkerOnlyDetail marker={marker} ordinal={markerOrdinalByArticleId.get(marker.article_id) ?? null} />;
    }
  }
  if (selection?.kind === "intelligence") {
    const card = overview.todays_intelligence.cards.find((c) => c.article_id === selection.articleId);
    if (card) {
      return <IntelligenceDetail card={card} ordinal={markerOrdinalByArticleId.get(card.article_id) ?? null} />;
    }
  }
  if (selection?.kind === "risk") {
    const row = overview.top_risks.rows.find((r) => r.risk.theme === selection.theme);
    if (row) {
      return <RiskDetail row={row} rankCount={overview.top_risks.rows.length} caption={overview.top_risks.caption} />;
    }
  }

  return (
    <aside className="ms-detail">
      <div className="ms-detail-empty">
        <p className="ms-empty-note">
          {overview.key_developments.rows.length === 0 &&
          overview.todays_intelligence.cards.length === 0 &&
          overview.top_risks.rows.length === 0
            ? overview.key_developments.empty_message
            : "Select a development, intelligence item, or risk to inspect its evidence."}
        </p>
      </div>
    </aside>
  );
}

function findMarker(overview: CompanyOverview, articleId: string): AnalyzedEvent | null {
  for (const timeframe of overview.chart.timeframes) {
    const marker = timeframe.markers.find((m) => m.article_id === articleId);
    if (marker) return marker;
  }
  return null;
}

/** A charted event that did not rank among the strongest Key Developments for this company. */
function MarkerOnlyDetail({ marker, ordinal }: { marker: AnalyzedEvent; ordinal: number | null }) {
  return (
    <aside className="ms-detail">
      <div className="ms-detail-header">
        <div className="ms-detail-header-row">
          <span className="ms-eyebrow">Analysed event</span>
          <span className="ms-num-qualifier ms-detail-ref">
            {ordinal !== null ? `marker ${ordinal}` : ""}
          </span>
        </div>
        <h2 className="ms-detail-title">{marker.summary}</h2>
        <div className="ms-qualifier">
          {formatDate(marker.event_date)} · {eventTypeLabel(marker.event_type)} ·{" "}
          {directionLabel(marker.direction)}
        </div>
      </div>
      <div className="ms-detail-body">
        <div className="ms-detail-section-body">
          <p className="ms-empty-note">
            This analysed event did not rank among the strongest key developments for this company, so its
            claims and evidence are not summarised here.
          </p>
        </div>
      </div>
      <div className="ms-detail-footer">
        <a className="ms-btn ms-btn-secondary" href={marker.article_url} target="_blank" rel="noreferrer">
          Open sources
        </a>
      </div>
    </aside>
  );
}

/**
 * Claims, corroboration, uncertainties, transmission channels, and related companies -- the
 * detail shared by every stored analysis, whether it reached the page as a grouped Key
 * Development or as a standalone Today's Intelligence card.
 */
function EventEvidenceSections({
  intelligenceEvent,
  corroboration,
}: {
  intelligenceEvent: CompanyIntelligenceEvent;
  corroboration: CorroborationView;
}) {
  const evidenceById = new Map(intelligenceEvent.evidence_sources.map((e) => [e.article_id, e]));
  const event = intelligenceEvent.event;

  return (
    <>
      {event.important_claims.length > 0 && (
        <>
          <div className="ms-detail-section-head">Important claims</div>
          <div className="ms-detail-section-body">
            <ul className="ms-bullet-list">
              {event.important_claims.map((claim, index) => (
                <li key={index}>{claim}</li>
              ))}
            </ul>
          </div>
        </>
      )}

      <div className="ms-detail-section-head">
        Claim evidence <span className="ms-detail-section-count">· {intelligenceEvent.claims.length}</span>
      </div>
      <div className="ms-detail-section-body">
        {intelligenceEvent.claims.length === 0 ? (
          <p className="ms-empty-note">No structured claims were extracted for this event.</p>
        ) : (
          intelligenceEvent.claims.map((claim) => (
            <div className="ms-claim" key={claim.claim_id}>
              <div className="ms-claim-head">
                <span className={`ms-tag ms-tag-${claimTone(claim.status)}`}>
                  {evidenceStatusLabel(claim.status)}
                </span>
                <span className="ms-num-qualifier">{Math.round(claim.confidence * 100)}% confidence</span>
              </div>
              <p className="ms-claim-reasoning">{claim.reasoning}</p>
              {claim.evidence_article_ids.length > 0 && (
                <div className="ms-claim-evidence">
                  {claim.evidence_article_ids.map((id) => {
                    const evidence = evidenceById.get(id);
                    if (!evidence) return null;
                    return (
                      <SourceRef
                        key={id}
                        publisher={evidence.publisher}
                        date={formatDate(evidence.published_at)}
                        url={evidence.url}
                        title={evidence.title}
                      />
                    );
                  })}
                </div>
              )}
            </div>
          ))
        )}
      </div>

      <div className="ms-detail-section-head">Corroboration</div>
      <div className="ms-detail-section-body">
        <p className="ms-qualifier">{corroboration.breakdown_label}</p>
        {corroboration.contradiction_label && (
          <div className="ms-contradiction">{corroboration.contradiction_label}</div>
        )}
      </div>

      {event.uncertainties.length > 0 && (
        <>
          <div className="ms-detail-section-head">Uncertainties</div>
          <div className="ms-detail-section-body">
            <ul className="ms-bullet-list">
              {event.uncertainties.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        </>
      )}

      {(event.positive_channels.length > 0 || event.negative_channels.length > 0) && (
        <>
          <div className="ms-detail-section-head">Possible transmission channels</div>
          <div className="ms-detail-section-body ms-channel-columns">
            <div>
              <div className="ms-eyebrow">Positive</div>
              {event.positive_channels.length === 0 ? (
                <p className="ms-qualifier">None supplied.</p>
              ) : (
                <ul className="ms-bullet-list">
                  {event.positive_channels.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <div className="ms-eyebrow">Negative</div>
              {event.negative_channels.length === 0 ? (
                <p className="ms-qualifier">None supplied.</p>
              ) : (
                <ul className="ms-bullet-list">
                  {event.negative_channels.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </>
      )}

      {intelligenceEvent.related_companies.length > 0 && (
        <>
          <div className="ms-detail-section-head">Related companies</div>
          <div className="ms-detail-section-body">
            {intelligenceEvent.related_companies.map((related) => (
              <div className="ms-related" key={related.ticker}>
                <span className="ms-related-ticker">{related.ticker}</span>{" "}
                <span className={`ms-tag ms-tag-${directionTone(related.possible_effect_direction)}`}>
                  {directionLabel(related.possible_effect_direction)}
                </span>
                <p className="ms-qualifier">{related.relationship_context}</p>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  );
}

function DevelopmentDetail({ row, ordinal }: { row: KeyDevelopmentView; ordinal: number | null }) {
  const otherMembers = row.members.filter((m) => m.article_id !== row.article_id);
  const event = row.event.event;

  return (
    <aside className="ms-detail">
      <div className="ms-detail-header">
        <div className="ms-detail-header-row">
          <span className="ms-eyebrow">Material development</span>
          <span className="ms-num-qualifier ms-detail-ref">
            {ordinal !== null ? `marker ${ordinal}` : "not charted this timeframe"}
          </span>
        </div>
        <h2 className="ms-detail-title">{event.summary}</h2>
        <a
          className="ms-development-source"
          href={row.event.source_reference.url}
          target="_blank"
          rel="noreferrer"
        >
          {row.event.source_reference.title}
        </a>
        <div className="ms-qualifier">
          {formatDate(row.event.source_reference.published_at)} · {row.provenance_note} ·{" "}
          {row.impact_label} · {horizonLabel(event.time_horizon)} horizon
        </div>
      </div>

      <div className="ms-detail-body">
        <EventEvidenceSections intelligenceEvent={row.event} corroboration={row.corroboration} />

        <div className="ms-detail-section-head">
          Also reported by <span className="ms-detail-section-count">· {otherMembers.length}</span>
        </div>
        <div className="ms-detail-section-body">
          {otherMembers.length === 0 ? (
            <p className="ms-empty-note">No other reports of this development were found.</p>
          ) : (
            otherMembers.map((member) => (
              <SourceRef
                key={member.article_id}
                publisher={member.source_reference.publisher}
                date={formatDate(member.source_reference.published_at)}
                url={member.source_reference.url}
                title={member.source_reference.title}
              />
            ))
          )}
        </div>
      </div>

      <div className="ms-detail-footer">
        <a className="ms-btn ms-btn-secondary" href={row.event.source_reference.url} target="_blank" rel="noreferrer">
          Open sources
        </a>
      </div>
    </aside>
  );
}

/**
 * One stored analysis, whether it reached the page as a ranked Top Intelligence card or was
 * opened directly from the article browser. Both carry the same server-owned labels, so the
 * parameter is the structural shape they share rather than either concrete type.
 */
function StoredAnalysisDetail({
  card,
  ordinal,
  eyebrow,
  sourceLabel,
}: {
  card: TodaysIntelligenceCardView | StoredArticleAnalysisView;
  ordinal: number | null;
  eyebrow: string;
  sourceLabel: string;
}) {
  const event = card.event.event;

  return (
    <aside className="ms-detail">
      <div className="ms-detail-header">
        <div className="ms-detail-header-row">
          <span className="ms-eyebrow">{eyebrow}</span>
          <span className="ms-num-qualifier ms-detail-ref">
            {ordinal !== null ? `marker ${ordinal}` : "not charted this timeframe"}
          </span>
        </div>
        <h2 className="ms-detail-title">{event.summary}</h2>
        <a
          className="ms-development-source"
          href={card.event.source_reference.url}
          target="_blank"
          rel="noreferrer"
        >
          {card.event.source_reference.title}
        </a>
        <div className="ms-qualifier">
          {formatDate(card.event.source_reference.published_at)} · {card.primary_source_label} ·{" "}
          {card.impact_label} · {horizonLabel(event.time_horizon)} horizon
        </div>
      </div>

      <div className="ms-detail-body">
        <EventEvidenceSections intelligenceEvent={card.event} corroboration={card.corroboration} />
      </div>

      <div className="ms-detail-footer">
        <a
          className="ms-btn ms-btn-secondary"
          href={card.event.source_reference.url}
          target="_blank"
          rel="noreferrer"
        >
          {sourceLabel}
        </a>
      </div>
    </aside>
  );
}

function IntelligenceDetail({ card, ordinal }: { card: TodaysIntelligenceCardView; ordinal: number | null }) {
  return (
    <StoredAnalysisDetail
      card={card}
      ordinal={ordinal}
      eyebrow="Top intelligence"
      sourceLabel="Open sources"
    />
  );
}

/**
 * A Relevant News row's stored analysis. Only rows the server marked analysed are selectable, so
 * a miss here means the analysis was retired between the list read and this one -- reported as
 * absent, never as something the reader could generate.
 */
function ArticleDetail({ state, ordinal }: { state: ArticleAnalysisState | null; ordinal: number | null }) {
  if (state === null || state.status === "loading") {
    return (
      <aside className="ms-detail">
        <div className="ms-detail-empty">
          <p className="ms-empty-note">Reading the stored analysis…</p>
        </div>
      </aside>
    );
  }
  if (state.status === "error") {
    return (
      <aside className="ms-detail">
        <div className="ms-detail-empty">
          <p className="ms-empty-note">{state.message}</p>
        </div>
      </aside>
    );
  }
  return (
    <StoredAnalysisDetail
      card={state.analysis}
      ordinal={ordinal}
      eyebrow="Stored analysis"
      sourceLabel="Original article"
    />
  );
}

function RiskDetail({
  row,
  rankCount,
  caption,
}: {
  row: RiskRowView;
  rankCount: number;
  caption: string;
}) {
  return (
    <aside className="ms-detail">
      <div className="ms-detail-header">
        <div className="ms-detail-header-row">
          <span className="ms-eyebrow">Ranked risk</span>
          <span className="ms-num-qualifier ms-detail-ref">
            rank {row.rank} of {rankCount}
          </span>
        </div>
        <h2 className="ms-detail-title">{row.label}</h2>
        <div className="ms-qualifier">
          updated {formatDate(row.risk.latest_published_at)} · first evidenced{" "}
          {formatDate(row.risk.first_evidenced_at)}
        </div>
      </div>

      <div className="ms-detail-risk-marker">
        <RiskMarker value={row.concern_index} color={row.band_color} width={120} />
        <span className="ms-qualifier">
          rank {row.rank} of {rankCount} · band {row.band}
        </span>
      </div>

      <div className="ms-detail-body">
        <div className="ms-detail-section-head">Summary</div>
        <div className="ms-detail-section-body">
          <p className="ms-prose">{row.risk.summary}</p>
        </div>

        <div className="ms-detail-section-head">
          Supporting evidence{" "}
          <span className="ms-detail-section-count">
            · {row.risk.supporting_signal_count} signal(s) · {row.risk.supporting_event_group_count} group(s)
          </span>
        </div>
        <div className="ms-detail-section-body">
          <SourceRef
            publisher={row.risk.primary_publisher}
            date={formatDate(row.risk.first_evidenced_at)}
            url={row.risk.primary_article_url}
            title="Primary source"
          />
          {row.risk.supporting_publishers.length > 0 && (
            <div className="ms-publisher-chips">
              {row.risk.supporting_publishers.map((publisher, index) => (
                <span className="ms-chip" key={`${publisher}-${index}`}>
                  {publisher}
                </span>
              ))}
            </div>
          )}
        </div>

        <p className="ms-pane-footnote">{caption}</p>
      </div>

      <div className="ms-detail-footer">
        <a
          className="ms-btn ms-btn-secondary"
          href={row.risk.primary_article_url}
          target="_blank"
          rel="noreferrer"
        >
          Open sources
        </a>
      </div>
    </aside>
  );
}

function claimTone(status: string): "positive" | "negative" | "neutral" {
  if (status === "corroborated") return "positive";
  if (status === "contradicted") return "negative";
  return "neutral";
}

function directionTone(direction: string): "positive" | "negative" | "neutral" {
  if (direction === "positive") return "positive";
  if (direction === "negative") return "negative";
  return "neutral";
}
