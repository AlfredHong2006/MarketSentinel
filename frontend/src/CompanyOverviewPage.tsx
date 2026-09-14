import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ApiNetworkError,
  ApiNotFoundError,
  ApiRefusedError,
  ApiServerError,
  fetchArticleAnalysis,
  fetchCompanyOverview,
  fetchRelevantNews,
  requestArticleAnalysis,
  requestCoverage,
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

/** The reader's own shared-request activity on this page: one company, any number of articles. */
interface RequestActivity {
  coverageSubmitting: boolean;
  coverageNote: string | null;
  articlesSubmitting: Set<string>;
  articleNote: string | null;
}

const IDLE_ACTIVITY: RequestActivity = {
  coverageSubmitting: false,
  coverageNote: null,
  articlesSubmitting: new Set(),
  articleNote: null,
};

// The zero-coverage explanations are deployment-state copy, like the loading and error views this
// client already owns — not an intelligence conclusion. They are deliberately mode-specific: the
// read-only claim is only true of a public deployment, so a private run keeps the server's own
// empty message instead.
const PUBLIC_NO_COVERAGE_MESSAGE =
  "No stored public coverage is available for this company yet. MarketSentinel supports this " +
  "company, but the public deployment is read-only and does not run new ingestion or AI " +
  "analysis. Try NVIDIA or Pfizer for prepared coverage.";
const REQUESTABLE_MESSAGE =
  "MarketSentinel supports this company but has not covered it yet. Starting coverage is " +
  "shared: the scheduled worker ingests its recent news and analyses material articles on its " +
  "next run (roughly every 6 hours), and everyone then sees the same result.";
const QUEUED_MESSAGE =
  "Coverage is queued for the next scheduled run (roughly every 6 hours). Results appear here " +
  "for everyone once they are published; nothing further is needed.";
const ACTIVE_AWAITING_MESSAGE =
  "Shared coverage is active for this company. Its first results appear after the next " +
  "scheduled run publishes.";

function describeRequestFailure(error: unknown): string {
  if (error instanceof ApiRefusedError) return error.message;
  if (error instanceof ApiNotFoundError) return error.message;
  if (error instanceof ApiServerError) return error.message;
  if (error instanceof ApiNetworkError) return error.message;
  return "The request could not be sent.";
}

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

  // The shared queue everyone sees comes from capabilities; the reader's own successful requests
  // are added locally so the page reflects them at once, without a second capabilities read.
  const [queuedCoverage, setQueuedCoverage] = useState<Set<string>>(new Set());
  const [queuedArticles, setQueuedArticles] = useState<Set<string>>(new Set());
  // Companies the server reported as already covered when the reader asked: the capabilities
  // snapshot this page was loaded with may predate the worker's activation.
  const [knownActive, setKnownActive] = useState<Set<string>>(new Set());
  const [activity, setActivity] = useState<RequestActivity>(IDLE_ACTIVITY);
  useEffect(() => {
    if (!capabilities) return;
    setQueuedCoverage((own) => new Set([...capabilities.pending_coverage_requests, ...own]));
    setQueuedArticles((own) => new Set([...capabilities.pending_article_requests, ...own]));
  }, [capabilities]);
  useEffect(() => setActivity(IDLE_ACTIVITY), [symbol]);
  // A request outcome belongs to the company it was made for. When the reader has moved to
  // another company before it settles, the shared queue is still updated (it is global) but the
  // note and the in-flight flag are not applied to the page now showing.
  const currentSymbol = useRef(symbol);
  useEffect(() => {
    currentSymbol.current = symbol;
  }, [symbol]);

  const supportsRequests = capabilities?.supports_coverage_requests === true;
  const coverageActive =
    capabilities?.covered_companies.includes(symbol) === true || knownActive.has(symbol);
  const coverageQueued = queuedCoverage.has(symbol);

  const startCoverage = useCallback(() => {
    const requestedFor = symbol;
    const settle = (note: string) => {
      if (currentSymbol.current !== requestedFor) return;
      setActivity((current) => ({ ...current, coverageSubmitting: false, coverageNote: note }));
    };
    setActivity((current) => ({ ...current, coverageSubmitting: true, coverageNote: null }));
    requestCoverage(requestedFor)
      .then((result) => {
        if (result.state === "covered") {
          setKnownActive((own) => new Set([...own, result.symbol]));
        } else {
          setQueuedCoverage((own) => new Set([...own, result.symbol]));
        }
        settle(result.message);
      })
      .catch((error: unknown) => settle(describeRequestFailure(error)));
  }, [symbol]);

  const requestAnalysis = useCallback(
    (articleId: string) => {
      const requestedFor = symbol;
      setActivity((current) => ({
        ...current,
        articlesSubmitting: new Set([...current.articlesSubmitting, articleId]),
        articleNote: null,
      }));
      const settle = (note: string) => {
        if (currentSymbol.current !== requestedFor) return;
        setActivity((current) => {
          const remaining = new Set(current.articlesSubmitting);
          remaining.delete(articleId);
          return { ...current, articlesSubmitting: remaining, articleNote: note };
        });
      };
      requestArticleAnalysis(requestedFor, articleId)
        .then((result) => {
          if (result.state !== "analysed") {
            setQueuedArticles((own) => new Set([...own, result.article_id]));
          }
          settle(result.message);
        })
        .catch((error: unknown) => settle(describeRequestFailure(error)));
    },
    [symbol],
  );

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
    // Four honest states, in precedence order: coverage already active but unpublished, a
    // queued shared request, a request the reader can make, or the plain read-only explanation.
    const empty = coverageActive
      ? { title: `Coverage active for ${symbol}`, message: ACTIVE_AWAITING_MESSAGE }
      : coverageQueued
        ? { title: `Coverage requested for ${symbol}`, message: QUEUED_MESSAGE }
        : supportsRequests
          ? { title: `No coverage yet for ${symbol}`, message: REQUESTABLE_MESSAGE }
          : {
              title: `No coverage yet for ${symbol}`,
              message:
                capabilities?.mode === "public"
                  ? PUBLIC_NO_COVERAGE_MESSAGE
                  : overview.key_developments.empty_message,
            };
    const offerStart = supportsRequests && !coverageActive && !coverageQueued;
    return (
      <div className={shellClass} style={shellStyle}>
        <AppHeader articlesIndexed={overview.coverage.articles} dataSource={overview.data_source} />
        <div className="ms-body-row">
          {rail}
          <EmptyOverviewView
            title={empty.title}
            message={empty.message}
            action={
              offerStart ? (
                <button
                  type="button"
                  className="ms-btn ms-btn-primary"
                  disabled={activity.coverageSubmitting}
                  onClick={startCoverage}
                >
                  {activity.coverageSubmitting ? "Requesting…" : "Start coverage"}
                </button>
              ) : coverageQueued ? (
                <span className="ms-chip">Queued</span>
              ) : undefined
            }
            note={activity.coverageNote}
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
              requests={{
                supported: supportsRequests,
                queued: queuedArticles,
                submitting: activity.articlesSubmitting,
                onRequest: requestAnalysis,
              }}
            />
          )}
          {activity.articleNote && (
            <p className="ms-empty-note" role="status">
              {activity.articleNote}
            </p>
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
