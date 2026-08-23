"""Minimal Streamlit client for the MarketSentinel FastAPI service."""

import os
from typing import Any

import pandas as pd
import requests
import streamlit as st

from marketsentinel.article_presentation import (
    EMPTY_RELATED_COMPANIES_MESSAGE,
    bullet_items,
)
from marketsentinel.dashboard_charts import (
    CHART_LAYERS,
    DEFAULT_TIMEFRAME,
    TIMEFRAME_MONTHS,
    build_combined_figure,
    build_price_figure,
    build_sentiment_figure,
    observed_sentiment_frame,
    price_frame_for_timeframe,
    select_meaningful_events,
    sentiment_coverage_note,
)
from marketsentinel.dashboard_event_state import (
    compatible_article_analysis_response,
    updated_event_markers,
    updated_intelligence_events,
)
from marketsentinel.dashboard_intelligence import (
    CompanyIntelligenceEvent,
    compatible_intelligence_events,
    contradiction_label,
    corroboration_label,
    corroboration_metric,
    evidence_breakdown_label,
    prepare_todays_intelligence,
    primary_source_label,
    summarize_corroboration,
)
from marketsentinel.dashboard_market_view import build_market_view
from marketsentinel.dashboard_risks import (
    CONCERN_INDEX_CAPTION,
    EMPTY_TOP_RISKS_MESSAGE,
    compatible_top_risks,
    prepare_top_risk_rows,
)

API_BASE_URL = os.getenv("MARKETSENTINEL_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = 180

st.set_page_config(
    page_title="MarketSentinel",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container {padding-top: 2rem; padding-bottom: 3rem;}
    [data-testid="stMetric"] {
        background: rgba(120, 120, 120, 0.06);
        border: 1px solid rgba(120, 120, 120, 0.18);
        border-radius: 0.75rem;
        padding: 1rem;
    }
    .source-pill {
        display: inline-block;
        border: 1px solid rgba(120, 120, 120, 0.25);
        border-radius: 999px;
        padding: 0.15rem 0.55rem;
        margin-right: 0.35rem;
        font-size: 0.78rem;
    }
    .company-price {
        text-align: right;
        padding-top: 0.4rem;
        font-size: 1.45rem;
        font-weight: 650;
    }
    .company-price small {
        display: block;
        color: rgba(120, 120, 120, 0.95);
        font-size: 0.78rem;
        font-weight: 400;
    }
    .price-positive { color: #15803d; }
    .price-negative { color: #b91c1c; }
    .risk-band-pill {
        display: inline-block;
        border: 1px solid;
        border-radius: 999px;
        padding: 0.1rem 0.6rem;
        font-size: 0.82rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=3_600, show_spinner=False)
def search_constituents(query: str, market: str) -> dict[str, Any]:
    response = requests.get(
        f"{API_BASE_URL}/api/v1/constituents/search",
        params={"q": query, "market": market, "limit": 30},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def request_analysis(symbol: str) -> dict[str, Any]:
    response = requests.post(
        f"{API_BASE_URL}/api/v1/analyze",
        json={"symbol": symbol},
        timeout=REQUEST_TIMEOUT,
    )
    if response.ok:
        return response.json()
    try:
        detail = response.json().get("detail", response.text)
    except requests.JSONDecodeError:
        detail = response.text
    raise RuntimeError(f"API returned {response.status_code}: {detail}")


def request_article_analysis(article_id: str) -> dict[str, Any]:
    response = requests.post(
        f"{API_BASE_URL}/api/v1/articles/analyze",
        json={"article_id": article_id},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def add_event_marker_to_current_analysis(response: dict[str, Any]) -> None:
    """Make a newly stored analysis visible on the next dashboard rerun."""

    current = st.session_state.get("analysis")
    if current is None:
        return
    updated_markers = updated_event_markers(
        response,
        current["constituent"]["symbol"],
        current.get("analyzed_events", []),
    )
    if updated_markers is None:
        return
    current["analyzed_events"] = updated_markers
    updated_intelligence = updated_intelligence_events(
        response,
        current["constituent"]["symbol"],
        current.get("intelligence_events", []),
    )
    if updated_intelligence is not None:
        current["intelligence_events"] = updated_intelligence


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_company_header(payload: dict[str, Any]) -> None:
    company = payload["constituent"]
    points = payload["price_history"]["points"]
    title_column, price_column = st.columns([3, 2])
    title_column.markdown(f"## {company['name']} ({company['symbol']})")
    if not points:
        price_column.markdown(
            '<div class="company-price">Price unavailable</div>', unsafe_allow_html=True
        )
        return
    latest = float(points[-1]["close"])
    delta = None
    if len(points) > 1 and float(points[-2]["close"]):
        delta = f"{(latest / float(points[-2]['close']) - 1):+.2%} last session"
    delta_class = "price-positive" if delta and not delta.startswith("-") else "price-negative"
    delta_markup = f'<small class="{delta_class}">{delta}</small>' if delta else ""
    price_column.markdown(
        f'<div class="company-price">{latest:,.2f}{delta_markup}</div>', unsafe_allow_html=True
    )


def render_company_chart(payload: dict[str, Any]) -> None:
    symbol = payload["constituent"]["symbol"]
    timeframe_column, view_column, layers_column = st.columns([4, 3, 5])
    with timeframe_column:
        timeframe = st.segmented_control(
            "Timeframe",
            list(TIMEFRAME_MONTHS),
            default=DEFAULT_TIMEFRAME,
            key=f"timeframe_{symbol}",
        )
    with view_column:
        view = st.segmented_control(
            "Layout",
            ["Combined", "Separate"],
            default="Combined",
            key=f"chart_layout_{symbol}",
        )
    with layers_column:
        layers = st.segmented_control(
            "Layers",
            list(CHART_LAYERS),
            selection_mode="multi",
            default=list(CHART_LAYERS),
            format_func=lambda layer: "News sentiment" if layer == "Sentiment" else layer,
            key=f"chart_layers_{symbol}",
        )
    if not layers:
        st.info("Select at least one chart layer.")
        return

    price_frame = price_frame_for_timeframe(payload["price_history"]["points"], timeframe)
    if price_frame.empty:
        st.warning("No price observations are available for this company.")
        return
    start, end = price_frame["date"].min(), price_frame["date"].max()
    sentiment_frame = observed_sentiment_frame(payload["daily_sentiment"], start, end)
    events = select_meaningful_events(payload.get("analyzed_events", []), start, end)

    if view == "Separate":
        if {"Price", "Events"}.intersection(layers):
            st.plotly_chart(
                build_price_figure(price_frame, events, layers),
                width="stretch",
                key=f"price_{timeframe}",
            )
        if "Sentiment" in layers:
            if sentiment_frame.empty:
                st.info("No genuine news-sentiment observations exist in this timeframe.")
            else:
                st.plotly_chart(
                    build_sentiment_figure(sentiment_frame),
                    width="stretch",
                    key=f"sentiment_{timeframe}",
                )
    else:
        st.plotly_chart(
            build_combined_figure(price_frame, sentiment_frame, events, layers),
            width="stretch",
            key=f"combined_{timeframe}",
        )
        if "Sentiment" in layers and sentiment_frame.empty:
            st.info("No genuine news-sentiment observations exist in this timeframe.")

    notes = [f"{len(price_frame)} trading-session price observations"]
    if "Events" in layers:
        notes.append(f"{len(events)} meaningful stored event markers")
    caption = " · ".join(notes) + "."
    if "Sentiment" in layers:
        coverage_note = sentiment_coverage_note(sentiment_frame, price_frame, timeframe)
        if coverage_note:
            caption += f" {coverage_note}"
    st.caption(caption)


def render_current_market_view(payload: dict[str, Any]) -> None:
    """Render four independent, deterministic observations — never a combined score/verdict."""

    st.subheader("Current Market View")
    intelligence_cards = prepare_todays_intelligence(
        compatible_intelligence_events(payload.get("intelligence_events", []))
    )
    risk_rows = prepare_top_risk_rows(compatible_top_risks(payload.get("top_risks", [])))
    summary = build_market_view(
        price_points=payload["price_history"]["points"],
        daily_sentiment=payload["daily_sentiment"],
        risk_rows=risk_rows,
        intelligence_cards=intelligence_cards,
    )
    for note in (
        summary.price_note,
        summary.sentiment_note,
        summary.risk_note,
        summary.intelligence_note,
    ):
        st.write(note)
    st.caption(
        "Each observation above is independent and descriptive only — not a combined "
        "score, verdict, or recommendation."
    )


def render_todays_intelligence(payload: dict[str, Any]) -> None:
    """Render only compatible, previously stored high-confidence event analyses."""

    st.subheader("Today's Intelligence")
    events = compatible_intelligence_events(payload.get("intelligence_events", []))
    cards = prepare_todays_intelligence(events)
    if not cards:
        st.info("No high-confidence analysed events available yet.")
        return
    st.caption(
        "Ordered by event magnitude, extraction confidence, evidence strength, source class, "
        "and publication time. These are event assessments, not price predictions."
    )
    for card in cards:
        item = card.event
        with st.container(border=True):
            st.markdown(f"**{item.source_reference.title}**")
            st.caption(
                f"{item.source_reference.publisher} · {item.source_reference.published_at:%d %b %Y}"
            )
            metric_columns = st.columns(4)
            metric_columns[0].metric(
                "Impact",
                card.impact_label,
                help=(
                    f"Magnitude score {card.impact_score}/100 — estimated qualitative "
                    "significance to this company; not a price-move probability."
                ),
            )
            metric_columns[1].metric(
                "Corroboration",
                card.corroboration_metric,
                help=(
                    "Distinct outside publishers whose articles a corroborated claim cited. "
                    "Absence means the supplied comparison articles did not substantiate a "
                    "claim, never that a claim is false."
                ),
            )
            metric_columns[2].metric("Direction", item.event.direction.value.title())
            metric_columns[3].metric(
                "Persistence",
                item.event.time_horizon.value.replace("_", " ").title(),
                help="Expected duration of the event's consequences, not time until it starts.",
            )
            st.write(item.event.summary)
            st.caption(f"Primary source: {card.primary_source_label} · {card.corroboration_label}")
            if card.contradiction_label:
                st.warning(card.contradiction_label)
            channel_columns = st.columns(2)
            with channel_columns[0]:
                st.markdown("**Possible positive channels**")
                render_evidence_list(item.event.positive_channels)
            with channel_columns[1]:
                st.markdown("**Possible negative channels**")
                render_evidence_list(item.event.negative_channels)
            if item.related_companies:
                st.markdown("**Related companies**")
                for related in item.related_companies:
                    st.write(
                        f"{related.company.name} ({related.ticker}) — "
                        f"{related.relationship_context}"
                    )
            with st.expander("View analysis"):
                render_event_analysis_details(item)
            st.markdown(f"[Original article]({item.source_reference.url})")


def render_top_risks(payload: dict[str, Any]) -> None:
    """Render ranked downside themes derived only from compatible stored analyses."""

    st.subheader("Top Risks")
    risks = compatible_top_risks(payload.get("top_risks", []))
    rows = prepare_top_risk_rows(risks)
    if not rows:
        st.info(EMPTY_TOP_RISKS_MESSAGE)
        return
    st.caption(CONCERN_INDEX_CAPTION)
    for row in rows:
        rank_column, label_column, score_column, band_column = st.columns([1, 8, 2, 3])
        rank_column.markdown(f"**{row.rank}**")
        label_column.markdown(f"**{row.label}**")
        score_column.markdown(f"**{row.concern_index}**")
        band_column.markdown(
            f'<span class="risk-band-pill" '
            f'style="background:{row.band_color}1a;color:{row.band_color};'
            f'border-color:{row.band_color}66">{row.band}</span>',
            unsafe_allow_html=True,
        )
        st.caption(
            f"{row.summary} · {row.risk.supporting_signal_count} supporting signal(s) "
            f"across {len(row.risk.supporting_publishers)} publisher(s) · latest "
            f"{row.risk.latest_published_at:%d %b %Y} · "
            f"[{row.risk.primary_publisher}]({row.risk.primary_article_url})"
        )


def render_ingestion_funnel(payload: dict[str, Any]) -> None:
    funnel = payload["ingestion_funnel"]
    st.subheader("Ingestion funnel")
    labels = ["Retrieved", "Relevant", "Unique", "Scored"]
    values = [funnel["retrieved"], funnel["relevant"], funnel["unique"], funnel["scored"]]
    columns = st.columns(4)
    for column, label, value in zip(columns, labels, values, strict=True):
        column.metric(label, value)
    st.caption(
        "Counts describe this refresh. Previously stored matching articles are retained without being "
        "rescored, so a repeated refresh can legitimately show zero newly scored items."
    )
    with st.expander("Funnel diagnostics"):
        diagnostics = {
            "Invalid/out-of-range dates": funnel["invalid_dates"],
            "Irrelevant": funnel["irrelevant"],
            "Invalid URLs": funnel["invalid_urls"],
            "Exact/provider-ID duplicates": funnel["exact_duplicates"],
            "Canonical-URL duplicates": funnel["canonical_url_duplicates"],
            "Title + publisher + time duplicates": funnel["near_title_duplicates"],
            "Database conflicts": funnel["database_conflicts"],
            "Excluded by request limit": funnel["request_limited"],
            "Previously scored": funnel["previously_scored"],
            "Provider failures": funnel["provider_failures"],
        }
        st.dataframe(
            pd.DataFrame(diagnostics.items(), columns=["Stage", "Count"]),
            hide_index=True,
            width="stretch",
        )
    automatic = payload.get("automatic_analysis")
    if automatic is not None:
        with st.expander("Automatic article analysis"):
            diagnostics = {
                "Considered": automatic["considered"],
                "Selected": automatic["selected"],
                "Demo articles rejected": automatic["demo_rejected"],
                "Below relevance floor": automatic["low_relevance_rejected"],
                "Obvious holding reports rejected": automatic["obvious_holdings_rejected"],
                "Predictions excluded": automatic["excluded_prediction"],
                "Commentary deprioritized": automatic["commentary_deprioritized"],
                "Publisher cap rejected": automatic["publisher_cap_rejected"],
                "Near-title variants rejected": automatic["near_title_rejected"],
                "Exact cache hits": automatic["cached"],
                "Newly generated": automatic["newly_generated"],
                "Failed": automatic["failed"],
                "Unavailable": automatic["unavailable"],
                "Skipped after budget/breaker": automatic["budget_skipped"],
            }
            st.dataframe(
                pd.DataFrame(diagnostics.items(), columns=["Stage", "Count"]),
                hide_index=True,
                width="stretch",
            )
            if automatic["circuit_breaker_tripped"]:
                st.caption("Stopped after two consecutive failed or unavailable responses.")


def render_forecast(payload: dict[str, Any]) -> None:
    forecast = payload["forecast"]
    metrics = forecast["metrics"]
    st.subheader("Experimental five-trading-day direction forecast")
    columns = st.columns(4)
    columns[0].metric("Model-estimated up probability", percent(forecast["probability_up"]))
    columns[1].metric("Validation accuracy", percent(metrics["validation_accuracy"]))
    columns[2].metric("Majority baseline", percent(metrics["majority_baseline_accuracy"]))
    columns[3].metric("Sentiment coverage", percent(forecast["sentiment_coverage"]))
    st.progress(forecast["probability_up"], text="Estimated probability of a higher adjusted close")

    if forecast["sentiment_features_used"]:
        st.success("The model had enough stored sentiment dates to include sentiment features.")
    else:
        st.warning(
            "The SQLite history does not yet contain enough genuine sentiment dates. This run is "
            "therefore a price/volume baseline; sentiment activates after sufficient coverage."
        )
    with st.expander("Model diagnostics and limitations"):
        st.write(forecast["warning"])
        st.write(
            {
                "as_of": forecast["as_of"],
                "training_samples": forecast["training_samples"],
                "validation_samples": metrics["validation_samples"],
                "momentum_baseline_accuracy": percent(metrics["momentum_baseline_accuracy"]),
                "ROC AUC": (
                    f"{metrics['roc_auc']:.3f}" if metrics["roc_auc"] is not None else "Unavailable"
                ),
                "features": forecast["features"],
            }
        )


DEFAULT_VISIBLE_ARTICLES = 5


def render_articles(payload: dict[str, Any]) -> None:
    st.subheader("Relevant News")
    articles = payload["articles"]
    if not articles:
        st.info("No scored articles are available for this 30-calendar-day range.")
        return

    frame = pd.DataFrame(articles)
    frame["publication_date"] = pd.to_datetime(frame["published_at"], utc=True).dt.date
    earliest, latest = frame["publication_date"].min(), frame["publication_date"].max()
    filter_columns = st.columns(2)
    selected_dates = filter_columns[0].date_input(
        "Publication dates (calendar)",
        value=(earliest, latest),
        min_value=earliest,
        max_value=latest,
    )
    sources = sorted(frame["source"].dropna().unique().tolist())
    selected_sources = filter_columns[1].multiselect("Sources", sources, default=sources)
    if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
        start_date, end_date = selected_dates
    else:
        start_date = end_date = selected_dates
    filtered = [
        article
        for article in articles
        if start_date <= pd.Timestamp(article["published_at"]).date() <= end_date
        and article["source"] in selected_sources
    ]
    st.caption(
        f"{len(filtered)} article(s) match the filters across "
        f"{len({pd.Timestamp(item['published_at']).date() for item in filtered})} calendar date(s)."
    )

    symbol = payload["constituent"]["symbol"]
    limit_key = f"article_limit_{symbol}"
    if limit_key not in st.session_state:
        st.session_state[limit_key] = DEFAULT_VISIBLE_ARTICLES
    visible_articles = filtered[: st.session_state[limit_key]]
    for article in visible_articles:
        demo = " · **DEMO DATA**" if article["is_demo"] else ""
        st.markdown(f"#### [{article['title']}]({article['url']})")
        st.caption(
            f"{article['source']} · {article['published_at']} · "
            f"{article['label'].title()} ({article['sentiment_score']:+.3f}){demo}"
        )
        st.markdown(
            f'<span class="source-pill">Positive {percent(article["positive"])}</span>'
            f'<span class="source-pill">Neutral {percent(article["neutral"])}</span>'
            f'<span class="source-pill">Negative {percent(article["negative"])}</span>',
            unsafe_allow_html=True,
        )
        with st.expander("Open AI-generated event analysis"):
            st.markdown(f"**Original headline:** [{article['title']}]({article['url']})")
            st.caption(
                f"{article['source']} · {article['published_at']} · stored article ID: {article['fingerprint']}"
            )
            st.info(
                "AI-generated event analysis — verify important claims against original sources."
            )
            event_key = f"article_event_{article['fingerprint']}"
            if st.button("Analyse this article", key=f"analyse_{article['fingerprint']}"):
                with st.spinner("Generating a bounded, structured event analysis…"):
                    try:
                        response = request_article_analysis(article["fingerprint"])
                        st.session_state[event_key] = response
                        add_event_marker_to_current_analysis(response)
                    except requests.RequestException as exc:
                        st.session_state[event_key] = {
                            "status": "failed",
                            "message": f"Article analysis request failed: {exc}",
                        }
            response = st.session_state.get(event_key)
            if response is not None:
                render_article_event_analysis(response)
        st.divider()
    if len(visible_articles) < len(filtered) and st.button(
        f"Show more ({len(filtered) - len(visible_articles)} remaining)"
    ):
        st.session_state[limit_key] += DEFAULT_VISIBLE_ARTICLES
        st.rerun()


def render_evidence_list(values: list[str]) -> None:
    for value in bullet_items(values):
        st.write(value)


def render_article_event_analysis(response: dict[str, Any]) -> None:
    typed_response = compatible_article_analysis_response(response)
    if typed_response is None:
        st.warning(
            "This stored analysis uses an incompatible legacy schema. Run the article analysis again "
            "after restarting the API to create a compatible result."
        )
        return
    status = typed_response.status
    if status in {"failed", "unavailable", "not_found"}:
        st.warning(typed_response.message or "Article analysis is unavailable.")
        return
    if typed_response.analysis is None:
        st.warning("The analysis response did not contain usable structured data.")
        return
    render_event_analysis_details(typed_response.analysis.to_company_intelligence_event())


def render_event_analysis_details(analysis: CompanyIntelligenceEvent) -> None:
    """Render typed event detail shared by fresh and stored dashboard views."""

    event = analysis.event
    columns = st.columns(6)
    columns[0].metric("Event", event.event_type.value.replace("_", " ").title())
    columns[1].metric("Direction", event.direction.value.title())
    columns[2].metric(
        "Possible magnitude",
        percent(event.magnitude),
        help=(
            "Estimated qualitative significance of the event to the selected company; "
            "not an expected stock-price move."
        ),
    )
    columns[3].metric("Time horizon", event.time_horizon.value.replace("_", " ").title())
    columns[4].metric(
        "Extraction confidence",
        percent(event.model_confidence),
        help="Confidence that Stage A correctly identified the event described by the supplied text.",
    )
    summary = summarize_corroboration(analysis)
    columns[5].metric(
        "Corroboration",
        corroboration_metric(summary),
        help=(
            "Distinct outside publishers whose articles a corroborated claim cited. Absence "
            "means the supplied comparison articles did not substantiate a claim, never that a "
            "claim is false."
        ),
    )
    st.markdown("**What happened**")
    st.write(event.summary)
    st.caption(
        "Primarily affected supported company: "
        f"{analysis.subject_company.name} ({analysis.subject_company.symbol}) "
        f"· Primary source: {primary_source_label(analysis.source_class)} "
        f"· {corroboration_label(summary)}"
    )
    if contradiction := contradiction_label(summary):
        st.warning(contradiction)
    if analysis.claims:
        st.markdown("**Claims and supplied-evidence assessment**")
        st.caption(evidence_breakdown_label(summary))
        evidence_titles = {item.article_id: item.title for item in analysis.evidence_sources}
        claim_rows = [
            {
                "Claim ID": item.claim_id,
                "Claim": _claim_text(item.claim_id, event.important_claims),
                "Evidence status": item.status.value,
                "Reasoning": item.reasoning,
                "Cited evidence": ", ".join(
                    evidence_titles.get(item_id, item_id) for item_id in item.evidence_article_ids
                )
                or "None",
            }
            for item in analysis.claims
        ]
        st.dataframe(pd.DataFrame(claim_rows), hide_index=True, width="stretch")
    if event.uncertainties:
        st.markdown("**Uncertainties**")
        for item in event.uncertainties:
            st.caption(f"• {item}")
    channel_columns = st.columns(2)
    with channel_columns[0]:
        st.markdown("**Possible positive channels**")
        render_evidence_list(event.positive_channels)
    with channel_columns[1]:
        st.markdown("**Possible negative channels**")
        render_evidence_list(event.negative_channels)
    if analysis.related_companies:
        st.markdown("**Related companies**")
        for item in analysis.related_companies:
            st.markdown(f"**{item.company.name} ({item.ticker}) — {item.relationship_context}**")
            st.write(f"Possible mechanism: {item.reasoning}")
            st.caption(
                f"Possible direction: {item.possible_effect_direction.value.title()} · "
                f"Confidence: {percent(item.confidence)}"
            )
    else:
        st.caption(EMPTY_RELATED_COMPANIES_MESSAGE)
    with st.expander("Research diagnostics"):
        st.caption(
            f"Evidence context score {percent(analysis.evidence_strength)} — a deterministic "
            "indicator of primary-source class and how much comparison material was supplied, "
            "used for ordering and risk support. It is not a corroboration count and not a "
            "probability that the article is true."
        )


def _claim_text(claim_id: str, claims: list[str]) -> str:
    """Show only a claim that exists in the typed event contract."""

    try:
        claim_index = int(claim_id.removeprefix("claim_")) - 1
    except ValueError:
        return claim_id
    return claims[claim_index] if 0 <= claim_index < len(claims) else claim_id


def render_health(payload: dict[str, Any]) -> None:
    with st.expander("Data-source health"):
        for source in payload["source_health"]:
            icon = {"healthy": "✅", "degraded": "⚠️", "unavailable": "❌"}[source["status"]]
            st.write(
                f"{icon} **{source['provider']}** — {source['status']} · "
                f"{source['valid_records']} valid records"
            )
            if source.get("message"):
                st.caption(source["message"])


st.title("MarketSentinel")
st.markdown(
    "**Company intelligence from market prices, financial-news sentiment, and analysed events.**"
)

with st.sidebar:
    st.header("Find a constituent")
    market = st.selectbox("Index", ["All", "S&P 500", "FTSE 100"])
    query = st.text_input(
        "Company or ticker", value="Apple", placeholder="e.g. AAPL or AstraZeneca"
    )

    results: dict[str, Any] | None = None
    if query.strip():
        try:
            results = search_constituents(query.strip(), market)
        except requests.RequestException:
            st.error(f"Cannot reach the API at {API_BASE_URL}. Start FastAPI first.")

    options = results["constituents"] if results else []
    if results and results["is_fallback"]:
        st.warning(results.get("message") or "A cached/fallback universe is in use.")
    selection = st.selectbox(
        "Match",
        options,
        format_func=lambda item: f"{item['symbol']} — {item['name']} ({item['market']})",
        disabled=not options,
    )
    analyze_clicked = st.button(
        "Run analysis",
        type="primary",
        width="stretch",
        disabled=selection is None,
    )
    st.caption(f"API: {API_BASE_URL}")
    st.caption(
        "Educational research only. Sentiment and forecast probabilities are not financial advice."
    )

if analyze_clicked and selection is not None:
    with st.spinner(
        "Fetching market/news data and scoring new headlines. First FinBERT use may download the model…"
    ):
        try:
            st.session_state.analysis = request_analysis(selection["symbol"])
        except (requests.RequestException, RuntimeError) as exc:
            st.error(str(exc))

if "analysis" not in st.session_state:
    st.info("Search for an index constituent and run an analysis to begin.")
else:
    analysis = st.session_state.analysis
    render_company_header(analysis)
    render_company_chart(analysis)
    render_current_market_view(analysis)
    render_todays_intelligence(analysis)
    render_top_risks(analysis)
    st.divider()
    render_articles(analysis)
    with st.expander("Research diagnostics", expanded=False):
        render_ingestion_funnel(analysis)
        render_forecast(analysis)
        render_health(analysis)
