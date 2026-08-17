"""Minimal Streamlit client for the MarketSentinel FastAPI service."""

import os
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

from marketsentinel.article_presentation import (
    EMPTY_RELATED_COMPANIES_MESSAGE,
    bullet_items,
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


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_price_chart(payload: dict[str, Any]) -> None:
    frame = pd.DataFrame(payload["price_history"]["points"])
    figure = go.Figure(
        go.Scatter(
            x=frame["date"],
            y=frame["close"],
            mode="lines",
            line={"color": "#2F80ED", "width": 2.5},
            hovertemplate="%{x}<br>%{y:.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        title="Last 30 trading sessions",
        xaxis_title=None,
        yaxis_title="Adjusted close",
        height=360,
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        hovermode="x unified",
    )
    st.plotly_chart(figure, use_container_width=True)


def render_sentiment_chart(payload: dict[str, Any]) -> None:
    values = payload["daily_sentiment"]
    if not values:
        st.info("No scored articles have been stored for this ticker yet.")
        return
    frame = pd.DataFrame(values)
    figure = make_subplots(specs=[[{"secondary_y": True}]])
    figure.add_trace(
        go.Bar(
            x=frame["date"],
            y=frame["article_count"],
            name="Articles",
            marker_color="rgba(130, 130, 150, 0.25)",
        ),
        secondary_y=True,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["score"],
            name="Daily sentiment",
            mode="lines+markers",
            line={"color": "#27AE60", "width": 2},
        ),
        secondary_y=False,
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["trend_3"],
            name="3-observation weighted trend",
            mode="lines",
            line={"color": "#F2994A", "width": 3},
            customdata=frame[["positive_share", "negative_share", "weighted_disagreement"]],
            hovertemplate=(
                "%{x}<br>Trend: %{y:.3f}<br>Positive share: %{customdata[0]:.1%}"
                "<br>Negative share: %{customdata[1]:.1%}<br>Disagreement: %{customdata[2]:.3f}"
                "<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    figure.add_hline(y=0, line_dash="dot", line_color="gray", secondary_y=False)
    figure.update_yaxes(title_text="Sentiment index (-1 to +1)", range=[-1, 1], secondary_y=False)
    figure.update_yaxes(title_text="Article count", rangemode="tozero", secondary_y=True)
    figure.update_layout(
        title="Historical daily sentiment (calendar dates with genuine scored articles)",
        height=390,
        margin={"l": 20, "r": 20, "t": 55, "b": 20},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.12},
    )
    st.plotly_chart(figure, use_container_width=True)
    coverage = len(frame)
    st.caption(
        f"Coverage: {coverage} calendar date(s) with scored articles in the requested 30-day backfill. "
        "Missing dates are absent, not neutral. Price uses trading sessions; sentiment uses calendar dates."
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
            use_container_width=True,
        )


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


def render_articles(payload: dict[str, Any]) -> None:
    st.subheader("Scored articles")
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
        st.session_state[limit_key] = 10
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
                        st.session_state[event_key] = request_article_analysis(
                            article["fingerprint"]
                        )
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
        st.session_state[limit_key] += 10
        st.rerun()


def render_evidence_list(values: list[str]) -> None:
    for value in bullet_items(values):
        st.write(value)


def render_article_event_analysis(response: dict[str, Any]) -> None:
    status = response["status"]
    if status in {"failed", "unavailable", "not_found"}:
        st.warning(response.get("message") or "Article analysis is unavailable.")
        return
    analysis = response.get("analysis")
    if analysis is None:
        st.warning("The analysis response did not contain usable structured data.")
        return
    st.caption("Result source: " + ("SQLite cache" if status == "cached" else "freshly generated"))
    event = analysis["event"]
    columns = st.columns(6)
    columns[0].metric("Event", event["event_type"].replace("_", " ").title())
    columns[1].metric("Direction", event["direction"].title())
    columns[2].metric(
        "Possible magnitude",
        percent(event["magnitude"]),
        help=(
            "Estimated qualitative significance of the event to the selected company; "
            "not an expected stock-price move."
        ),
    )
    columns[3].metric("Time horizon", event["time_horizon"].replace("_", " ").title())
    columns[4].metric(
        "Extraction confidence",
        percent(event["model_confidence"]),
        help="Confidence that Stage A correctly identified the event described by the supplied text.",
    )
    columns[5].metric(
        "Evidence strength",
        percent(analysis["evidence_strength"]),
        help=(
            "Deterministic indicator of supplied-evidence quality and corroboration; "
            "not a probability that the article is true."
        ),
    )
    st.markdown("**What happened**")
    st.write(event["summary"])
    st.caption(
        "Primarily affected supported company: "
        f"{analysis['subject_company']['name']} ({analysis['subject_company']['symbol']}) "
        f"· Source type: {analysis['source_class'].replace('_', ' ')}"
    )
    if analysis["claims"]:
        st.markdown("**Claims and supplied-evidence assessment**")
        evidence_titles = {
            item["article_id"]: item["title"] for item in analysis["evidence_sources"]
        }
        claim_rows = [
            {
                "Claim ID": item["claim_id"],
                "Claim": event["important_claims"][
                    int(item["claim_id"].removeprefix("claim_")) - 1
                ],
                "Evidence status": item["status"],
                "Reasoning": item["reasoning"],
                "Supporting sources": ", ".join(
                    evidence_titles.get(item_id, item_id)
                    for item_id in item["evidence_article_ids"]
                )
                or "None",
                "Confidence": percent(item["confidence"]),
            }
            for item in analysis["claims"]
        ]
        st.dataframe(pd.DataFrame(claim_rows), hide_index=True, use_container_width=True)
    if event["uncertainties"]:
        st.markdown("**Uncertainties**")
        for item in event["uncertainties"]:
            st.caption(f"• {item}")
    channel_columns = st.columns(2)
    with channel_columns[0]:
        st.markdown("**Possible positive channels**")
        render_evidence_list(event["positive_channels"])
    with channel_columns[1]:
        st.markdown("**Possible negative channels**")
        render_evidence_list(event["negative_channels"])
    if analysis["related_companies"]:
        st.markdown("**Related companies**")
        for item in analysis["related_companies"]:
            st.markdown(
                f"**{item['company']['name']} ({item['ticker']}) — {item['relationship_context']}**"
            )
            st.write(f"Possible mechanism: {item['reasoning']}")
            st.caption(
                f"Possible direction: {item['possible_effect_direction'].title()} · "
                f"Confidence: {percent(item['confidence'])}"
            )
    else:
        st.caption(EMPTY_RELATED_COMPANIES_MESSAGE)
    st.caption(
        "Model: {model} · Evidence records: {evidence} · Schema: {schema} · Created: {created}".format(
            model=analysis["model_version"],
            evidence=analysis["evidence_count"],
            schema=analysis["schema_version"],
            created=analysis["analysis_created_at"],
        )
    )
    with st.expander("Structured-data debug view"):
        st.json(analysis)


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
    "**Recent financial-news sentiment, stored over time, with an experimental direction baseline.**"
)
st.warning(
    "Educational research only. Sentiment and forecast probabilities are not financial advice and "
    "do not predict exact prices."
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
        use_container_width=True,
        disabled=selection is None,
    )
    st.caption(f"API: {API_BASE_URL}")

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
    company = analysis["constituent"]
    st.header(f"{company['name']} · {company['symbol']}")
    render_health(analysis)
    price_column, sentiment_column = st.columns(2)
    with price_column:
        render_price_chart(analysis)
    with sentiment_column:
        render_sentiment_chart(analysis)
    render_ingestion_funnel(analysis)
    render_forecast(analysis)
    render_articles(analysis)
