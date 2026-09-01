import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiNetworkError,
  ApiNotFoundError,
  ApiServerError,
  fetchArticleAnalysis,
  fetchCompanyOverview,
  fetchRelevantNews,
} from "./api/client";
import type { CapabilitiesView, CompanyOverview, RelevantNewsView } from "./api/types";
import type { ArticleAnalysisState } from "./components/DetailPane";
import { AppHeader } from "./components/AppHeader";
import { ChartPane } from "./components/ChartPane";
import { CompanySearch } from "./components/CompanySearch";
import { DetailPane } from "./components/DetailPane";
import { DevelopmentsPane } from "./components/DevelopmentsPane";
import { IdentityHeader } from "./components/IdentityHeader";
import { MetricStrip } from "./components/MetricStrip";
import { RailResizer } from "./components/RailResizer";
import { RelevantNewsPane } from "./components/RelevantNewsPane";
import { RisksPane } from "./components/RisksPane";
import { EmptyOverviewView, ErrorView, LoadingView } from "./components/StateViews";
import { TodaysIntelligencePane } from "./components/TodaysIntelligencePane";
import { UtilityStrip } from "./components/UtilityStrip";
import type { Selection } from "./selection";
import { useResizableRails } from "./useResizableRails";

type LoadState =
  | { status: "loading" }
  | { status: "error"; title: string; message: string }
  | { status: "ready"; overview: CompanyOverview };

type RelevantNewsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: RelevantNewsView };

// The zero-coverage explanation is deployment-state copy, like the loading and error views this
// client already owns — not an intelligence conclusion. It is deliberately mode-specific: the
// read-only claim is only true of a public deployment, so a private run keeps the server's own
// empty message instead.
const PUBLIC_NO_COVERAGE_MESSAGE =
  "No stored public coverage is available for this company yet. MarketSentinel supports this " +
  "company, but the public deployment is read-only and does not run new ingestion or AI " +
  "analysis. Try NVIDIA or Pfizer for prepared coverage.";

export function CompanyOverviewPage({
  symbol,
  onSymbolChange,
  capabilities,
}: {
  symbol: string;
  onSymbolChange: (symbol: string) => void;
  capabilities: CapabilitiesView | null;
}) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [relevantNews, setRelevantNews] = useState<RelevantNewsState>({ status: "loading" });
  const [articleAnalysis, setArticleAnalysis] = useState<ArticleAnalysisState | null>(null);
  const [selectedTimeframe, setSelectedTimeframe] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [requestId, setRequestId] = useState(0);
  const rails = useResizableRails();

  // Applied as the same custom properties the stylesheet already uses for the two rail widths,
  // so the centre workspace reflows through the existing flex rules.
  const shellStyle = {
    "--rail-w": `${rails.railWidth}px`,
    "--detail-w": `${rails.detailWidth}px`,
  } as React.CSSProperties;
  const shellClass = `ms-shell${rails.isDragging ? " ms-shell-resizing" : ""}`;

  const load = useCallback(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchCompanyOverview(symbol, controller.signal)
      .then((overview) => {
        setState({ status: "ready", overview });
        setSelectedTimeframe(overview.chart.default_timeframe);
        setSelection(firstSelection(overview));
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (error instanceof ApiNotFoundError) {
          // A 404 here means the symbol is not in the constituent universe at all — a company
          // that merely has nothing stored still returns 200 with an empty overview.
          setState({ status: "error", title: `${symbol} was not found`, message: error.message });
        } else if (error instanceof ApiServerError) {
          setState({ status: "error", title: "The overview could not be read", message: error.message });
        } else if (error instanceof ApiNetworkError) {
          setState({
            status: "error",
            title: "Could not reach the MarketSentinel API",
            message: `${error.message} Confirm the API is running and reachable from this browser.`,
          });
        } else {
          setState({ status: "error", title: "Something went wrong", message: String(error) });
        }
      });
    return () => controller.abort();
  }, [symbol]);

  useEffect(() => load(), [load, requestId]);

  useEffect(() => {
    const controller = new AbortController();
    setRelevantNews({ status: "loading" });
    fetchRelevantNews(symbol, controller.signal)
      .then((data) => setRelevantNews({ status: "ready", data }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setRelevantNews({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => controller.abort();
  }, [symbol, requestId]);

  // Lazy: one stored analysis is read only when a reader actually opens that row, rather than
  // inlining every analysis into the article list payload.
  const articleId = selection?.kind === "article" ? selection.articleId : null;
  useEffect(() => {
    if (articleId === null) {
      setArticleAnalysis(null);
      return;
    }
    const controller = new AbortController();
    setArticleAnalysis({ status: "loading" });
    fetchArticleAnalysis(symbol, articleId, controller.signal)
      .then((analysis) => setArticleAnalysis({ status: "ready", analysis }))
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setArticleAnalysis({
          status: "error",
          message:
            error instanceof ApiNotFoundError
              ? "No stored analysis is available for this article."
              : error instanceof Error
                ? error.message
                : String(error),
        });
      });
    return () => controller.abort();
  }, [symbol, articleId]);

  const retry = useCallback(() => setRequestId((id) => id + 1), []);

  const markerOrdinalByArticleId = useMemo(() => {
    const map = new Map<string, number>();
    if (state.status !== "ready" || !selectedTimeframe) return map;
    const active = state.overview.chart.timeframes.find((tf) => tf.timeframe === selectedTimeframe);
    active?.markers.forEach((marker, index) => map.set(marker.article_id, index + 1));
    return map;
  }, [state, selectedTimeframe]);

  const rail = (
    <>
      <CompanySearch currentSymbol={symbol} onSelect={onSymbolChange} capabilities={capabilities} />
      <RailResizer
        side="left"
        label="Search rail width"
        onPointerDown={rails.startRailDrag}
        onReset={rails.resetRail}
      />
    </>
  );

  if (state.status === "loading") {
    return (
      <div className={shellClass} style={shellStyle}>
        <AppHeader articlesIndexed={null} dataSource={null} />
        <div className="ms-body-row">
          {rail}
          <LoadingView symbol={symbol} />
        </div>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className={shellClass} style={shellStyle}>
        <AppHeader articlesIndexed={null} dataSource={null} />
        <div className="ms-body-row">
          {rail}
          <ErrorView title={state.title} message={state.message} onRetry={retry} />
        </div>
      </div>
    );
  }

  const { overview } = state;
  const hasAnyCoverage =
    overview.coverage.articles > 0 ||
    overview.key_developments.rows.length > 0 ||
    overview.top_risks.rows.length > 0;

  if (!hasAnyCoverage) {
    return (
      <div className={shellClass} style={shellStyle}>
        <AppHeader articlesIndexed={overview.coverage.articles} dataSource={overview.data_source} />
        <div className="ms-body-row">
          {rail}
          <EmptyOverviewView
            symbol={symbol}
            message={
              capabilities?.mode === "public"
                ? PUBLIC_NO_COVERAGE_MESSAGE
                : overview.key_developments.empty_message
            }
          />
        </div>
      </div>
    );
  }

  const annotation =
    selection?.kind === "development"
      ? (() => {
          const row = overview.key_developments.rows.find((r) => r.article_id === selection.articleId);
          if (!row) return null;
          return {
            ordinal: markerOrdinalByArticleId.get(row.article_id) ?? null,
            text: row.event.event.summary,
            meta: row.provenance_note,
          };
        })()
      : selection?.kind === "intelligence"
        ? (() => {
            const card = overview.todays_intelligence.cards.find((c) => c.article_id === selection.articleId);
            if (!card) return null;
            return {
              ordinal: markerOrdinalByArticleId.get(card.article_id) ?? null,
              text: card.event.event.summary,
              meta: card.primary_source_label,
            };
          })()
        : null;

  return (
    <div className={shellClass} style={shellStyle}>
      <AppHeader articlesIndexed={overview.coverage.articles} dataSource={overview.data_source} />
      <div className="ms-body-row">
        {rail}
        <div className="ms-main-column">
          <div className="ms-identity-block">
            <IdentityHeader constituent={overview.constituent} generatedAt={overview.generated_at} />
            <MetricStrip marketView={overview.market_view} coverage={overview.coverage} />
            <p className="ms-disclaimer">{overview.disclaimer}</p>
          </div>

          <TodaysIntelligencePane
            todaysIntelligence={overview.todays_intelligence}
            selectedArticleId={selection?.kind === "intelligence" ? selection.articleId : null}
            onSelect={(articleId) => setSelection({ kind: "intelligence", articleId })}
          />

          <div className="ms-split-row">
            <DevelopmentsPane
              keyDevelopments={overview.key_developments}
              markerOrdinalByArticleId={markerOrdinalByArticleId}
              selectedArticleId={selection?.kind === "development" ? selection.articleId : null}
              onSelect={(articleId) => setSelection({ kind: "development", articleId })}
            />
            <RisksPane
              topRisks={overview.top_risks}
              selectedTheme={selection?.kind === "risk" ? selection.theme : null}
              onSelect={(theme) => setSelection({ kind: "risk", theme })}
            />
          </div>

          <ChartPane
            chart={overview.chart}
            symbol={overview.constituent.symbol}
            selectedTimeframe={selectedTimeframe ?? overview.chart.default_timeframe}
            onTimeframeChange={setSelectedTimeframe}
            onMarkerSelect={(articleId) => setSelection({ kind: "development", articleId })}
            annotation={annotation}
            // Dragging a rail changes the chart's available width without any window resize, so
            // the chart is told to re-measure rather than left waiting on a ResizeObserver.
            resizeKey={`${rails.railWidth}x${rails.detailWidth}`}
          />

          {relevantNews.status === "ready" && (
            <RelevantNewsPane
              relevantNews={relevantNews.data}
              selectedArticleId={selection?.kind === "article" ? selection.articleId : null}
              onSelect={(id) => setSelection({ kind: "article", articleId: id })}
            />
          )}
          {relevantNews.status === "error" && (
            <p className="ms-empty-note">Relevant news could not be read: {relevantNews.message}</p>
          )}
        </div>

        <RailResizer
          side="right"
          label="Detail rail width"
          onPointerDown={rails.startDetailDrag}
          onReset={rails.resetDetail}
        />
        <DetailPane
          overview={overview}
          selection={selection}
          markerOrdinalByArticleId={markerOrdinalByArticleId}
          articleAnalysis={articleAnalysis}
        />
      </div>
      <UtilityStrip
        articlesIndexed={overview.coverage.articles}
        materialCount={overview.key_developments.diagnostics.material}
        generatedAt={overview.generated_at}
        horizon={selectedTimeframe ?? overview.chart.default_timeframe}
      />
    </div>
  );
}

function firstSelection(overview: CompanyOverview): Selection | null {
  if (overview.key_developments.rows.length > 0) {
    return { kind: "development", articleId: overview.key_developments.rows[0]!.article_id };
  }
  if (overview.top_risks.rows.length > 0) {
    return { kind: "risk", theme: overview.top_risks.rows[0]!.risk.theme };
  }
  return null;
}
