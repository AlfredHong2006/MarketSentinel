"""Assembly of the Company Overview surface from already-derived, deterministic parts.

This module makes no judgements of its own. It owns no threshold, no ordering, no gate, and no
user-facing vocabulary: every verdict, rank, count and label below is produced by the module that
already owns it -- ``materiality`` for what is a development and in what order, the corroboration
semantics in ``dashboard_intelligence`` for what may be called external support, ``dashboard_risks``
for risk row presentation, ``dashboard_market_view`` for the four independent observations, and
``dashboard_charts`` for which stored analyses clear the shared meaningful-event floor on a chart.

Its only job is to move the *call site* of those functions from a client process to the API
boundary, so a non-Python client can read the product's conclusions instead of re-deriving them and
silently forking the single source of truth. Nothing here is persisted: like ``materiality`` and
``rank_company_risks``, every value recomputes from stored analyses on each request, so a policy
change needs no migration and no re-analysis.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from marketsentinel.dashboard_charts import (
    DEFAULT_TIMEFRAME,
    TIMEFRAME_MONTHS,
    observed_sentiment_frame,
    price_frame_for_timeframe,
    select_meaningful_events,
    sentiment_coverage_note,
)
from marketsentinel.dashboard_intelligence import (
    CorroborationSummary,
    corroboration_label,
    evidence_breakdown_label,
    prepare_todays_intelligence,
)
from marketsentinel.dashboard_market_view import build_market_view
from marketsentinel.dashboard_risks import (
    CONCERN_INDEX_CAPTION,
    EMPTY_TOP_RISKS_MESSAGE,
    prepare_top_risk_rows,
)
from marketsentinel.domain import (
    AnalyzedEvent,
    ChartTimeframeView,
    ChartView,
    CompanyIntelligenceEvent,
    CompanyOverview,
    Constituent,
    CorroborationView,
    CoverageView,
    DailySentiment,
    KeyDevelopmentsView,
    KeyDevelopmentView,
    MarketViewSummaryView,
    MaterialityDiagnosticsView,
    PriceHistory,
    RiskRowView,
    ScoredArticle,
    TopRisksView,
)
from marketsentinel.materiality import (
    EMPTY_KEY_DEVELOPMENTS_MESSAGE,
    KeyDevelopmentRow,
    key_developments_caption,
    prepare_key_developments,
)
from marketsentinel.risk_scoring import RiskRanking

# Mirrors the wording the Streamlit chart already uses when a company has no usable price series.
# An absent series is reported as absent; it is never replaced with a substitute or a flat line.
NO_PRICE_OBSERVATIONS_MESSAGE = "No price observations are available for this company."


def build_company_overview(
    *,
    constituent: Constituent,
    price_history: PriceHistory | None,
    price_message: str | None = None,
    articles: Sequence[ScoredArticle],
    daily_sentiment: Sequence[DailySentiment],
    analyzed_events: Sequence[AnalyzedEvent],
    intelligence_events: Sequence[CompanyIntelligenceEvent],
    risks: RiskRanking,
    coverage_window_days: int,
    generated_at: datetime,
    disclaimer: str,
    data_source: Literal["stored", "refreshed"] = "stored",
) -> CompanyOverview:
    """Project one company's stored analyses into the Company Overview contract."""

    price_points = _as_payload(price_history.points if price_history is not None else [])
    sentiment_points = _as_payload(daily_sentiment)
    developments = prepare_key_developments(intelligence_events)
    risk_rows = prepare_top_risk_rows(risks.top_risks)
    market_view = build_market_view(
        price_points=price_points,
        daily_sentiment=sentiment_points,
        risk_rows=risk_rows,
        intelligence_cards=prepare_todays_intelligence(intelligence_events),
    )
    return CompanyOverview(
        constituent=constituent,
        data_source=data_source,
        generated_at=generated_at,
        coverage=CoverageView(
            articles=len(articles),
            analysed_articles=len(intelligence_events),
            window_days=coverage_window_days,
            latest_sentiment=daily_sentiment[-1] if daily_sentiment else None,
        ),
        market_view=MarketViewSummaryView(
            price_note=market_view.price_note,
            sentiment_note=market_view.sentiment_note,
            risk_note=market_view.risk_note,
            intelligence_note=market_view.intelligence_note,
        ),
        chart=_chart_view(
            price_history=price_history,
            price_message=price_message,
            price_points=price_points,
            sentiment_points=sentiment_points,
            daily_sentiment=daily_sentiment,
            analyzed_events=analyzed_events,
        ),
        key_developments=KeyDevelopmentsView(
            rows=[_development_view(row) for row in developments.rows],
            diagnostics=MaterialityDiagnosticsView(
                considered=developments.diagnostics.considered,
                material=developments.diagnostics.material,
                developments=developments.diagnostics.developments,
                rendered=developments.diagnostics.rendered,
                rejected=developments.diagnostics.rejected,
                rejected_by_condition=dict(developments.diagnostics.rejected_by_condition),
            ),
            caption=key_developments_caption(developments.diagnostics),
            empty_message=EMPTY_KEY_DEVELOPMENTS_MESSAGE,
        ),
        top_risks=TopRisksView(
            rows=[
                RiskRowView(
                    rank=row.rank,
                    label=row.label,
                    concern_index=row.concern_index,
                    band=row.risk.band,
                    band_color=row.band_color,
                    summary=row.summary,
                    risk=row.risk,
                )
                for row in risk_rows
            ],
            diagnostics=risks.diagnostics,
            caption=CONCERN_INDEX_CAPTION,
            empty_message=EMPTY_TOP_RISKS_MESSAGE,
        ),
        disclaimer=disclaimer,
    )


def _development_view(row: KeyDevelopmentRow) -> KeyDevelopmentView:
    """Serialise one prepared row. Labels already on the row are reused, never re-derived."""

    return KeyDevelopmentView(
        article_id=row.event.article_id,
        event=row.event,
        members=list(row.group.members),
        publisher_count=row.group.publisher_count,
        impact_label=row.impact_label,
        # The same expression the intelligence card uses for its displayed 0-100 impact score.
        impact_score=round(row.event.event.magnitude * 100),
        tier_label=row.tier_label,
        primary_source_label=row.primary_source_label,
        provenance_note=row.provenance_note,
        corroboration=_corroboration_view(
            row.corroboration,
            metric_label=row.corroboration_metric,
            contradiction=row.contradiction_label,
        ),
    )


def _corroboration_view(
    summary: CorroborationSummary,
    *,
    metric_label: str,
    contradiction: str | None,
) -> CorroborationView:
    return CorroborationView(
        total_claims=summary.total_claims,
        corroborated_claims=summary.corroborated_claims,
        contradicted_claims=summary.contradicted_claims,
        unresolved_claims=summary.unresolved_claims,
        comparison_articles=summary.comparison_articles,
        supporting_articles=summary.supporting_articles,
        external_sources=summary.external_sources,
        primary_is_official=summary.primary_is_official,
        metric_label=metric_label,
        summary_label=corroboration_label(summary),
        contradiction_label=contradiction,
        breakdown_label=evidence_breakdown_label(summary),
    )


def _chart_view(
    *,
    price_history: PriceHistory | None,
    price_message: str | None,
    price_points: Sequence[Mapping[str, Any]],
    sentiment_points: Sequence[Mapping[str, Any]],
    daily_sentiment: Sequence[DailySentiment],
    analyzed_events: Sequence[AnalyzedEvent],
) -> ChartView:
    """Describe the observed series, plus the marker set each supported timeframe admits."""

    if price_history is None or not price_points:
        return ChartView(
            status="unavailable",
            message=price_message or NO_PRICE_OBSERVATIONS_MESSAGE,
            daily_sentiment=list(daily_sentiment),
            default_timeframe=DEFAULT_TIMEFRAME,
        )
    event_payload = _as_payload(analyzed_events)
    events_by_id = {event.article_id: event for event in analyzed_events}
    return ChartView(
        status="available",
        source=price_history.source,
        fetched_at=price_history.fetched_at,
        points=list(price_history.points),
        daily_sentiment=list(daily_sentiment),
        default_timeframe=DEFAULT_TIMEFRAME,
        timeframes=[
            _timeframe_view(
                timeframe=timeframe,
                price_points=price_points,
                sentiment_points=sentiment_points,
                event_payload=event_payload,
                events_by_id=events_by_id,
            )
            for timeframe in TIMEFRAME_MONTHS
        ],
    )


def _timeframe_view(
    *,
    timeframe: str,
    price_points: Sequence[Mapping[str, Any]],
    sentiment_points: Sequence[Mapping[str, Any]],
    event_payload: Sequence[Mapping[str, Any]],
    events_by_id: Mapping[str, AnalyzedEvent],
) -> ChartTimeframeView:
    price_frame = price_frame_for_timeframe(price_points, timeframe)
    if price_frame.empty:
        return ChartTimeframeView(timeframe=timeframe)
    start, end = price_frame["date"].min(), price_frame["date"].max()
    sentiment_frame = observed_sentiment_frame(sentiment_points, start, end)
    selected = select_meaningful_events(event_payload, start, end)
    return ChartTimeframeView(
        timeframe=timeframe,
        start_date=start.date(),
        end_date=end.date(),
        price_observations=len(price_frame),
        sentiment_observations=len(sentiment_frame),
        # Mapped back to typed markers in the order the selector returned them, so the shared
        # meaningful-event floor and its ordering stay the only thing deciding what a chart shows.
        markers=[
            events_by_id[article_id]
            for item in selected
            if (article_id := str(item["article_id"])) in events_by_id
        ],
        sentiment_coverage_note=sentiment_coverage_note(sentiment_frame, price_frame, timeframe),
    )


def _as_payload(values: Sequence[Any]) -> list[dict[str, Any]]:
    """Render typed records as the JSON-shaped mappings the pure dashboard helpers accept."""

    return [value.model_dump(mode="json") for value in values]
