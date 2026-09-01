import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { AnalyzedEvent, ChartTimeframeView, ChartView, PricePoint } from "../api/types";
import { directionLabel, eventTypeLabel, formatDate, formatPrice } from "../format";
import { useElementSize } from "../useElementSize";
import { Pane } from "./Pane";

/**
 * Vertical headroom added above and below the visible price extremes, as a fraction of their
 * span. Without it the axis domain hugs the data exactly and the line — plus the marker glyphs,
 * which are drawn above their own point — sit hard against the plot edges.
 */
const PRICE_AXIS_PADDING = 0.08;

/** A flat series has no span to take a percentage of, so it falls back to an absolute nudge. */
const FLAT_SERIES_PADDING = 0.5;

/** Below this many rows a range slider is noise rather than navigation. */
const MIN_ROWS_FOR_BRUSH = 12;

interface ChartRow {
  date: string;
  close?: number;
  score?: number;
  articleCount?: number;
  positiveShare?: number;
  negativeShare?: number;
  weightedDisagreement?: number;
  markerPrice?: number;
  markerOrdinal?: number;
  markerDirection?: string;
  markerEventType?: string;
  markerSummary?: string;
  markerMagnitude?: number;
  markerConfidence?: number;
  markerArticleId?: string;
}

interface Annotation {
  ordinal: number | null;
  text: string;
  meta: string;
}

interface BrushRange {
  startIndex: number;
  endIndex: number;
}

function directionColor(direction: string | undefined): string {
  if (direction === "positive") return "var(--sentiment-pos)";
  if (direction === "negative") return "var(--sentiment-neg)";
  return "var(--ink-2)";
}

function sentimentBarColor(score: number | undefined): string {
  return (score ?? 0) >= 0 ? "var(--sentiment-pos)" : "var(--sentiment-neg)";
}

/**
 * The price-axis domain, derived from the observations actually visible right now — the selected
 * timeframe narrowed by any brush range — plus padding.
 *
 * Recharts' `"auto"` domain fits the data exactly, which is what let the line reach (and, in a
 * short pane, leave) the top and bottom edges of the plotting area. Marker prices are included so
 * an event sitting at an extreme is inside the axis too. Nothing is fabricated: the domain is
 * derived from real closes, and no value is invented to fill it.
 */
function priceDomainFor(rows: ChartRow[]): [number, number] | undefined {
  let low = Number.POSITIVE_INFINITY;
  let high = Number.NEGATIVE_INFINITY;
  for (const row of rows) {
    for (const value of [row.close, row.markerPrice]) {
      if (value === undefined || !Number.isFinite(value)) continue;
      if (value < low) low = value;
      if (value > high) high = value;
    }
  }
  if (!Number.isFinite(low) || !Number.isFinite(high)) return undefined;
  const span = high - low;
  const padding = span > 0 ? span * PRICE_AXIS_PADDING : FLAT_SERIES_PADDING;
  return [low - padding, high + padding];
}

/**
 * Round tick values *inside* an already-derived domain.
 *
 * Rounding the domain itself outward to nice bounds would waste vertical space — on a wide range
 * it can round a 226-point span out to 0–300 and shrink the line into the middle of the plot. So
 * the domain stays tight and only the labels are rounded, which keeps the axis readable without
 * costing the series any height.
 */
function niceTicks(low: number, high: number, target = 5): number[] {
  const span = high - low;
  if (!(span > 0)) return [];
  const raw = span / target;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const normalised = raw / magnitude;
  const step = (normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10) * magnitude;
  const ticks: number[] = [];
  for (let value = Math.ceil(low / step) * step; value <= high; value += step) {
    ticks.push(Number(value.toFixed(6)));
  }
  return ticks;
}

function nearestClose(points: PricePoint[], targetDate: string): number | undefined {
  if (points.length === 0) return undefined;
  const targetTime = new Date(targetDate).getTime();
  let best = points[0]!;
  let bestDiff = Math.abs(new Date(points[0]!.date).getTime() - targetTime);
  for (const point of points) {
    const diff = Math.abs(new Date(point.date).getTime() - targetTime);
    if (diff < bestDiff) {
      bestDiff = diff;
      best = point;
    }
  }
  return best.close;
}

function buildChartRows(
  points: PricePoint[],
  sentiment: ChartView["daily_sentiment"],
  markers: AnalyzedEvent[],
  start: string,
  end: string,
): ChartRow[] {
  const byDate = new Map<string, ChartRow>();
  const ensure = (date: string): ChartRow => {
    let row = byDate.get(date);
    if (!row) {
      row = { date };
      byDate.set(date, row);
    }
    return row;
  };

  const windowedPoints = points.filter((p) => p.date >= start && p.date <= end);
  for (const point of windowedPoints) {
    ensure(point.date).close = point.close;
  }
  for (const s of sentiment) {
    if (s.date < start || s.date > end) continue;
    const row = ensure(s.date);
    row.score = s.score;
    row.articleCount = s.article_count;
    row.positiveShare = s.positive_share;
    row.negativeShare = s.negative_share;
    row.weightedDisagreement = s.weighted_disagreement;
  }
  markers.forEach((marker, index) => {
    const row = ensure(marker.event_date);
    row.markerOrdinal = index + 1;
    row.markerDirection = marker.direction;
    row.markerEventType = marker.event_type;
    row.markerSummary = marker.summary;
    row.markerMagnitude = marker.magnitude;
    row.markerConfidence = marker.extraction_confidence;
    row.markerArticleId = marker.article_id;
    row.markerPrice = row.close ?? nearestClose(windowedPoints, marker.event_date);
  });

  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function MarkerDot(props: {
  cx?: number;
  cy?: number;
  payload?: ChartRow;
  onSelect: (articleId: string) => void;
}) {
  const { cx, cy, payload, onSelect } = props;
  if (cx === undefined || cy === undefined || !payload?.markerArticleId || !payload.markerOrdinal) {
    return null;
  }
  const size = 5 + 5 * (payload.markerMagnitude ?? 0.5);
  const color = directionColor(payload.markerDirection);
  return (
    <g
      transform={`translate(${cx},${cy})`}
      className="ms-chart-marker"
      tabIndex={0}
      role="button"
      aria-label={`Marker ${payload.markerOrdinal}: ${directionLabel(payload.markerDirection ?? "uncertain")} ${payload.markerEventType ?? ""}`}
      onClick={() => onSelect(payload.markerArticleId!)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(payload.markerArticleId!);
        }
      }}
    >
      <rect
        x={-size}
        y={-size}
        width={size * 2}
        height={size * 2}
        transform="rotate(45)"
        className="ms-chart-marker-box"
        stroke={color}
      />
      <text y={-size - 4} textAnchor="middle" className="ms-chart-marker-label">
        {payload.markerOrdinal}
      </text>
    </g>
  );
}

function ChartTooltip({ active, payload, label }: { active?: boolean; payload?: { payload: ChartRow }[]; label?: string }) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0]!.payload;
  return (
    <div className="ms-chart-tooltip">
      <div className="ms-chart-tooltip-date">{formatDate(label ?? row.date)}</div>
      {row.close !== undefined && (
        <div className="ms-chart-tooltip-row">
          <span>Price</span>
          <span className="ms-num-qualifier">{formatPrice(row.close)}</span>
        </div>
      )}
      {row.score !== undefined && (
        <div className="ms-chart-tooltip-row">
          <span>Sentiment</span>
          <span className="ms-num-qualifier">
            {row.score >= 0 ? "+" : ""}
            {row.score.toFixed(3)}
          </span>
        </div>
      )}
      {row.articleCount !== undefined && (
        <div className="ms-qualifier">
          {row.articleCount} scored article(s) · {Math.round((row.positiveShare ?? 0) * 100)}% positive ·{" "}
          {Math.round((row.negativeShare ?? 0) * 100)}% negative
        </div>
      )}
      {row.markerOrdinal !== undefined && (
        <div className="ms-chart-tooltip-marker">
          <div className="ms-chart-tooltip-row">
            <strong>Marker {row.markerOrdinal}</strong>
            <span>{directionLabel(row.markerDirection ?? "uncertain")}</span>
          </div>
          <div className="ms-qualifier">{eventTypeLabel(row.markerEventType ?? "")}</div>
          <p className="ms-qualifier ms-chart-tooltip-summary">{row.markerSummary}</p>
        </div>
      )}
    </div>
  );
}

/**
 * MAX is served for every company, but it is only worth a button where it actually reaches
 * further back than the longest calendar preset. Otherwise it is a second control that does
 * exactly what 1Y already does.
 */
function visibleTimeframes(timeframes: ChartTimeframeView[]): ChartTimeframeView[] {
  const longestPreset = timeframes
    .filter((tf) => tf.timeframe !== "MAX")
    .reduce((most, tf) => Math.max(most, tf.price_observations), 0);
  return timeframes.filter(
    (tf) => tf.timeframe !== "MAX" || tf.price_observations > longestPreset,
  );
}

export function ChartPane({
  chart,
  symbol,
  selectedTimeframe,
  onTimeframeChange,
  onMarkerSelect,
  annotation,
  resizeKey,
}: {
  chart: ChartView;
  symbol: string;
  selectedTimeframe: string;
  onTimeframeChange: (timeframe: string) => void;
  onMarkerSelect: (articleId: string) => void;
  annotation: Annotation | null;
  /** Changes whenever the surrounding layout is resized by the app itself (a rail drag). */
  resizeKey?: unknown;
}) {
  const [showSentiment, setShowSentiment] = useState(true);
  const [brush, setBrush] = useState<BrushRange | null>(null);
  // Recharts' Brush keeps its traveller positions in internal state and does not move them back
  // when the index props change, so clearing the range alone resets the chart while leaving the
  // slider visibly zoomed. Keying the chart on this remounts it, which is what actually returns
  // the travellers -- a key on the Brush alone is not enough, because Recharts clones its
  // children rather than reconciling them directly.
  const [brushNonce, setBrushNonce] = useState(0);
  const [canvasRef, canvasSize] = useElementSize<HTMLDivElement>(resizeKey);

  const active = chart.timeframes.find((tf) => tf.timeframe === selectedTimeframe);
  const rows = useMemo(() => {
    if (!active?.start_date || !active.end_date) return [];
    return buildChartRows(chart.points, chart.daily_sentiment, active.markers, active.start_date, active.end_date);
  }, [chart.points, chart.daily_sentiment, active]);

  // A brush range indexes into `rows`, so switching timeframe (or any reload that changes the row
  // set) must clear it rather than carry stale indices into a different series.
  // A different timeframe is a different row set, so any existing range indexes into data that no
  // longer exists. Remounting via the nonce also returns the travellers to full width.
  useEffect(() => {
    setBrush(null);
    setBrushNonce((n) => n + 1);
  }, [selectedTimeframe, rows.length]);

  const resetZoom = useCallback(() => {
    setBrush(null);
    setBrushNonce((n) => n + 1);
  }, []);

  const visibleRows = useMemo(() => {
    if (!brush) return rows;
    return rows.slice(brush.startIndex, brush.endIndex + 1);
  }, [rows, brush]);

  const priceDomain = useMemo(() => priceDomainFor(visibleRows), [visibleRows]);
  const priceTicks = useMemo(
    () => (priceDomain ? niceTicks(priceDomain[0], priceDomain[1]) : undefined),
    [priceDomain],
  );
  const hasSentiment = rows.some((row) => row.score !== undefined);
  const showBrush = rows.length >= MIN_ROWS_FOR_BRUSH;
  const buttons = useMemo(() => visibleTimeframes(chart.timeframes), [chart.timeframes]);

  const zoomNote =
    brush && visibleRows.length > 0
      ? `zoomed: ${formatDate(visibleRows[0]!.date)} – ${formatDate(visibleRows[visibleRows.length - 1]!.date)}`
      : null;

  if (chart.status === "unavailable") {
    return (
      <Pane title={`${symbol} price`} className="ms-chart-pane">
        <p className="ms-empty-note">{chart.message}</p>
      </Pane>
    );
  }

  return (
    <Pane
      title={`${symbol} price`}
      meta={
        active
          ? `${active.price_observations} observations · basis: close${
              zoomNote ? ` · ${zoomNote}` : ""
            }${active.sentiment_coverage_note ? ` · ${active.sentiment_coverage_note}` : ""}`
          : (chart.source ?? undefined)
      }
      controls={
        <>
          {brush && (
            <button type="button" className="ms-toggle-btn ms-toggle-btn-active" onClick={resetZoom}>
              Reset zoom
            </button>
          )}
          {hasSentiment && (
            <button
              type="button"
              className={`ms-toggle-btn${showSentiment ? " ms-toggle-btn-active" : ""}`}
              onClick={() => setShowSentiment((v) => !v)}
              aria-pressed={showSentiment}
            >
              Sentiment
            </button>
          )}
          <div className="ms-time-range" role="group" aria-label="Chart timeframe">
            {buttons.map((tf) => (
              <button
                key={tf.timeframe}
                type="button"
                className={`ms-time-range-btn${tf.timeframe === selectedTimeframe ? " ms-time-range-btn-active" : ""}`}
                onClick={() => onTimeframeChange(tf.timeframe)}
                disabled={tf.price_observations === 0}
              >
                {tf.timeframe}
              </button>
            ))}
          </div>
        </>
      }
      className="ms-chart-pane"
    >
      <div className="ms-chart-body">
        <div className="ms-chart-plot">
          {rows.length >= 2 ? (
            <>
              <div className="ms-chart-legend" aria-hidden="true">
                <span className="ms-chart-legend-item">
                  <span className="ms-chart-legend-swatch-line" />
                  Price
                </span>
                {showSentiment && hasSentiment && (
                  <span className="ms-chart-legend-item">
                    <span className="ms-chart-legend-swatch-bars">
                      <span className="ms-chart-legend-swatch-bar-pos" />
                      <span className="ms-chart-legend-swatch-bar-neg" />
                    </span>
                    Sentiment
                  </span>
                )}
              </div>
              <div className="ms-chart-canvas" ref={canvasRef}>
                {canvasSize.width > 0 && canvasSize.height > 0 && (
                  <ComposedChart
                    key={brushNonce}
                    width={canvasSize.width}
                    height={canvasSize.height}
                    data={rows}
                    margin={{ top: 22, right: 16, left: 14, bottom: 4 }}
                  >
                    <CartesianGrid stroke="var(--chart-grid)" vertical={false} />
                    <XAxis
                      dataKey="date"
                      tickFormatter={formatDate}
                      tick={{ fill: "var(--chart-axis)", fontSize: 10, fontFamily: "var(--font-numeric)" }}
                      axisLine={{ stroke: "var(--border-hairline)" }}
                      tickLine={false}
                      minTickGap={48}
                    />
                    <YAxis
                      yAxisId="price"
                      orientation="right"
                      // Derived from the visible closes and marker prices with headroom, so the
                      // series can never reach the edge of the plotting area.
                      domain={priceDomain ?? ["auto", "auto"]}
                      ticks={priceTicks && priceTicks.length > 0 ? priceTicks : undefined}
                      tick={{ fill: "var(--chart-axis)", fontSize: 10, fontFamily: "var(--font-numeric)" }}
                      axisLine={false}
                      tickLine={false}
                      width={52}
                      tickFormatter={(v: number) => v.toFixed(0)}
                    />
                    {showSentiment && hasSentiment && (
                      // Sentiment keeps its own fixed, independent scale: it is a bounded index,
                      // not a quantity that should rescale with whatever price is doing.
                      <YAxis
                        yAxisId="sentiment"
                        orientation="left"
                        domain={[-1, 1]}
                        tick={{ fill: "var(--chart-axis)", fontSize: 10, fontFamily: "var(--font-numeric)" }}
                        axisLine={false}
                        tickLine={false}
                        width={32}
                      />
                    )}
                    <Tooltip content={<ChartTooltip />} />
                    {showSentiment && hasSentiment && (
                      <ReferenceLine yAxisId="sentiment" y={0} stroke="var(--border-emphasis)" strokeDasharray="2 2" />
                    )}
                    {showSentiment && hasSentiment && (
                      // Sentiment is a bounded daily aggregate, not a continuous quantity, so it
                      // reads as diverging columns off the zero line -- a different visual
                      // encoding from the continuous price line, not just a different colour.
                      <Bar yAxisId="sentiment" dataKey="score" maxBarSize={4} isAnimationActive={false} name="Sentiment">
                        {rows.map((row) => (
                          <Cell key={row.date} fill={sentimentBarColor(row.score)} />
                        ))}
                      </Bar>
                    )}
                    <Line
                      yAxisId="price"
                      type="monotone"
                      dataKey="close"
                      stroke="var(--series-1)"
                      strokeWidth={1.6}
                      dot={false}
                      // Rows are the union of price dates, sentiment dates and marker dates, and
                      // sentiment is scored on calendar days while price exists only on trading
                      // days. Every weekend therefore contributes a row with no close, which would
                      // otherwise break the line into disconnected segments. Connecting joins
                      // consecutive real observations; it displays no value that was not observed
                      // -- no dot is drawn and the tooltip still gates Price on `close`.
                      connectNulls
                      isAnimationActive={false}
                      name="Price"
                    />
                    <Scatter
                      yAxisId="price"
                      dataKey="markerPrice"
                      isAnimationActive={false}
                      shape={(props: object) => <MarkerDot {...props} onSelect={onMarkerSelect} />}
                    />
                    {showBrush && (
                      <Brush
                        dataKey="date"
                        height={16}
                        travellerWidth={8}
                        stroke="var(--border-emphasis)"
                        fill="var(--surface-sunken)"
                        tickFormatter={() => ""}
                        startIndex={brush?.startIndex ?? 0}
                        endIndex={brush?.endIndex ?? rows.length - 1}
                        onChange={(range: { startIndex?: number; endIndex?: number }) => {
                          const start = range.startIndex ?? 0;
                          const end = range.endIndex ?? rows.length - 1;
                          // A full-width selection is not a zoom; treat it as cleared so the
                          // Reset control does not linger with nothing to reset.
                          setBrush(start <= 0 && end >= rows.length - 1 ? null : { startIndex: start, endIndex: end });
                        }}
                      />
                    )}
                  </ComposedChart>
                )}
              </div>
            </>
          ) : (
            <p className="ms-empty-note">No price observations are available for this window.</p>
          )}
        </div>
        <div className="ms-chart-annotation">
          {annotation ? (
            <>
              <p className="ms-annotation-text">{annotation.text}</p>
              <div className="ms-qualifier ms-annotation-meta">
                {annotation.ordinal !== null ? `marker ${annotation.ordinal} · ` : ""}
                {annotation.meta}
              </div>
            </>
          ) : (
            <p className="ms-qualifier">
              {chart.source ?? "Price source unavailable"}
              {chart.fetched_at ? ` · fetched ${formatDate(chart.fetched_at)}` : ""}
              {showBrush ? " · drag the slider below the chart to zoom" : ""}
            </p>
          )}
        </div>
      </div>
    </Pane>
  );
}
