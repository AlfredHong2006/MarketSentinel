"""Pure chart preparation and Plotly construction for the company dashboard."""

from collections.abc import Collection, Mapping, Sequence
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from marketsentinel.event_policy import is_meaningful_event_values

TIMEFRAME_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}
DEFAULT_TIMEFRAME = "6M"
CHART_LAYERS = ("Price", "Sentiment", "Events")
MAX_EVENT_MARKERS = 8

_PRICE_COLOR = "#4C78FF"
_SENTIMENT_COLOR = "#22C55E"
_EVENT_COLORS = {
    "positive": "#16A34A",
    "negative": "#DC2626",
    "mixed": "#F59E0B",
    "neutral": "#64748B",
    "uncertain": "#94A3B8",
}


def price_frame_for_timeframe(points: Sequence[Mapping[str, Any]], timeframe: str) -> pd.DataFrame:
    """Return genuine price observations within the selected calendar window."""

    if timeframe not in TIMEFRAME_MONTHS:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    frame = pd.DataFrame(points)
    if frame.empty:
        return pd.DataFrame(columns=["date", "close", "volume"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    cutoff = frame["date"].max() - pd.DateOffset(months=TIMEFRAME_MONTHS[timeframe])
    return frame.loc[frame["date"] >= cutoff].reset_index(drop=True)


def observed_sentiment_frame(
    values: Sequence[Mapping[str, Any]], start: pd.Timestamp, end: pd.Timestamp
) -> pd.DataFrame:
    """Filter observed sentiment without resampling or manufacturing missing dates."""

    frame = pd.DataFrame(values)
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "score",
                "trend_3",
                "article_count",
                "positive_share",
                "negative_share",
                "weighted_disagreement",
            ]
        )
    frame["date"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    return frame.loc[frame["date"].between(start, end)].reset_index(drop=True)


def sentiment_coverage_note(
    sentiment_frame: pd.DataFrame, price_frame: pd.DataFrame, timeframe: str
) -> str | None:
    """Describe the real observed sentiment date span; never implies interpolation or fabrication."""

    if sentiment_frame.empty or price_frame.empty:
        return None
    window_start = price_frame["date"].min()
    earliest = sentiment_frame["date"].min()
    latest = sentiment_frame["date"].max()
    observed_dates = len(sentiment_frame)
    span_days = (latest - earliest).days + 1
    has_gaps = observed_dates < span_days
    note = (
        f"Sentiment observations span {earliest:%d %b} to {latest:%d %b}"
        f" ({observed_dates} observed date{'s' if observed_dates != 1 else ''}"
        f"{', with gaps' if has_gaps else ''})."
    )
    if earliest > window_start:
        note += f" Earlier dates in this {timeframe} view have no sentiment data."
    note += " Points are observed only, never interpolated or filled."
    return note


def select_meaningful_events(
    events: Sequence[Mapping[str, Any]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    limit: int = MAX_EVENT_MARKERS,
) -> list[dict[str, Any]]:
    """Select a bounded set of material, sufficiently reliable stored analyses."""

    candidates: list[dict[str, Any]] = []
    for event in events:
        event_date = pd.Timestamp(event["event_date"])
        magnitude = float(event["magnitude"])
        extraction_confidence = float(event["extraction_confidence"])
        if not start <= event_date <= end:
            continue
        if not is_meaningful_event_values(
            str(event["event_type"]), magnitude, extraction_confidence
        ):
            continue
        candidate = dict(event)
        candidate["event_date"] = event_date
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            float(item["magnitude"]),
            float(item["extraction_confidence"]),
            float(item["evidence_strength"]),
            item["event_date"],
        ),
        reverse=True,
    )
    return sorted(candidates[:limit], key=lambda item: item["event_date"])


def _event_marker_frame(
    events: Sequence[Mapping[str, Any]], price_frame: pd.DataFrame
) -> pd.DataFrame:
    if not events or price_frame.empty:
        return pd.DataFrame()
    rows = []
    for event in events:
        event_date = pd.Timestamp(event["event_date"])
        nearest_index = (price_frame["date"] - event_date).abs().idxmin()
        summary = str(event["summary"])
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "..."
        rows.append(
            {
                **event,
                "event_date": event_date,
                "marker_price": float(price_frame.loc[nearest_index, "close"]),
                "event_label": str(event["event_type"]).replace("_", " ").title(),
                "direction_label": str(event["direction"]).title(),
                "hover_summary": summary,
            }
        )
    return pd.DataFrame(rows)


def _add_price_trace(figure: go.Figure, frame: pd.DataFrame, *, secondary_y: bool) -> None:
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["close"],
            name="Price",
            mode="lines",
            line={"color": _PRICE_COLOR, "width": 2.8},
            hovertemplate="%{x|%d %b %Y}<br>Price: %{y:,.2f}<extra></extra>",
        ),
        secondary_y=secondary_y,
    )


def _add_sentiment_trace(figure: go.Figure, frame: pd.DataFrame, *, secondary_y: bool) -> None:
    customdata = frame[
        ["article_count", "positive_share", "negative_share", "weighted_disagreement"]
    ]
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["score"],
            name="News sentiment",
            mode="lines+markers",
            connectgaps=False,
            line={"color": _SENTIMENT_COLOR, "width": 2},
            marker={"size": 6},
            customdata=customdata,
            hovertemplate=(
                "%{x|%d %b %Y}<br>News sentiment: %{y:+.3f}"
                "<br>Scored articles: %{customdata[0]}"
                "<br>Positive share: %{customdata[1]:.0%}"
                "<br>Negative share: %{customdata[2]:.0%}"
                "<br>Disagreement: %{customdata[3]:.3f}<extra></extra>"
            ),
        ),
        secondary_y=secondary_y,
    )


def _add_event_trace(figure: go.Figure, frame: pd.DataFrame, *, secondary_y: bool) -> None:
    customdata = frame[
        [
            "event_label",
            "hover_summary",
            "direction_label",
            "magnitude",
            "extraction_confidence",
        ]
    ]
    figure.add_trace(
        go.Scatter(
            x=frame["event_date"],
            y=frame["marker_price"],
            name="Analysed events",
            mode="markers",
            marker={
                "color": [
                    _EVENT_COLORS.get(str(direction), _EVENT_COLORS["uncertain"])
                    for direction in frame["direction"]
                ],
                "size": [10 + 10 * float(value) for value in frame["magnitude"]],
                "symbol": "diamond",
                "line": {"color": "white", "width": 1.2},
            },
            customdata=customdata,
            hovertemplate=(
                "<b>%{customdata[0]}</b> · %{x|%d %b %Y}<br>"
                "%{customdata[1]}<br>Direction: %{customdata[2]}"
                " · Possible magnitude: %{customdata[3]:.0%}"
                "<br>Extraction confidence: %{customdata[4]:.0%}<extra></extra>"
            ),
        ),
        secondary_y=secondary_y,
    )


def build_combined_figure(
    price_frame: pd.DataFrame,
    sentiment_frame: pd.DataFrame,
    events: Sequence[Mapping[str, Any]],
    layers: Collection[str],
) -> go.Figure:
    """Build one price-dominant chart with independent sentiment and event overlays."""

    price_axis_visible = bool({"Price", "Events"}.intersection(layers))
    figure = make_subplots(specs=[[{"secondary_y": price_axis_visible}]])
    if "Price" in layers:
        _add_price_trace(figure, price_frame, secondary_y=False)
    if "Events" in layers:
        event_frame = _event_marker_frame(events, price_frame)
        if not event_frame.empty:
            _add_event_trace(figure, event_frame, secondary_y=False)
    if "Sentiment" in layers and not sentiment_frame.empty:
        _add_sentiment_trace(figure, sentiment_frame, secondary_y=price_axis_visible)
        figure.add_hline(
            y=0,
            line_dash="dot",
            line_color="rgba(100, 116, 139, 0.7)",
            secondary_y=price_axis_visible,
        )
        figure.update_yaxes(
            title_text="News sentiment",
            range=[-1, 1],
            secondary_y=price_axis_visible,
        )
    if price_axis_visible:
        figure.update_yaxes(title_text="Price", secondary_y=False)
    _style_figure(figure, height=470)
    return figure


def build_price_figure(
    price_frame: pd.DataFrame,
    events: Sequence[Mapping[str, Any]],
    layers: Collection[str],
) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": False}]])
    if "Price" in layers:
        _add_price_trace(figure, price_frame, secondary_y=False)
    if "Events" in layers:
        event_frame = _event_marker_frame(events, price_frame)
        if not event_frame.empty:
            _add_event_trace(figure, event_frame, secondary_y=False)
    figure.update_yaxes(title_text="Price", secondary_y=False)
    _style_figure(figure, height=430)
    return figure


def build_sentiment_figure(sentiment_frame: pd.DataFrame) -> go.Figure:
    figure = make_subplots(specs=[[{"secondary_y": False}]])
    _add_sentiment_trace(figure, sentiment_frame, secondary_y=False)
    figure.add_hline(y=0, line_dash="dot", line_color="rgba(100, 116, 139, 0.7)")
    figure.update_yaxes(title_text="News sentiment", range=[-1, 1], secondary_y=False)
    _style_figure(figure, height=300)
    return figure


def _style_figure(figure: go.Figure, *, height: int) -> None:
    figure.update_xaxes(title_text=None, showgrid=False)
    figure.update_layout(
        height=height,
        margin={"l": 20, "r": 20, "t": 30, "b": 20},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 0},
        plot_bgcolor="rgba(0, 0, 0, 0)",
        paper_bgcolor="rgba(0, 0, 0, 0)",
    )
