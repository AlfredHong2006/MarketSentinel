import type React from "react";
import { useMemo, useState } from "react";
import type { ArticleRowView, RelevantNewsView } from "../api/types";
import { formatDate, sentimentLabel } from "../format";
import { Tag } from "./controls";
import { Pane } from "./Pane";

type SentimentFilter = "all" | "positive" | "negative" | "neutral";
type AnalysedFilter = "all" | "analysed" | "not_analysed";

function toDay(iso: string): string {
  return iso.slice(0, 10);
}

/**
 * The read-only Relevant News browser: every stored, sentiment-scored article for this company.
 * GET /api/v1/companies/{symbol}/articles only -- there is no "analyse this article" action here,
 * on this surface or anywhere in this client. Filtering is client-side over the already-fetched,
 * bounded article list; nothing here re-derives a materiality or ranking verdict.
 */
export function RelevantNewsPane({
  relevantNews,
  selectedArticleId,
  onSelect,
}: {
  relevantNews: RelevantNewsView;
  selectedArticleId: string | null;
  onSelect: (articleId: string) => void;
}) {
  const articles = relevantNews.articles;
  const sources = useMemo(
    () => [...new Set(articles.map((item) => item.source))].sort((a, b) => a.localeCompare(b)),
    [articles],
  );
  const bounds = useMemo(() => {
    if (articles.length === 0) return null;
    const days = articles.map((item) => toDay(item.published_at));
    return { earliest: days.reduce((a, b) => (a < b ? a : b)), latest: days.reduce((a, b) => (a > b ? a : b)) };
  }, [articles]);

  const [source, setSource] = useState("all");
  const [sentiment, setSentiment] = useState<SentimentFilter>("all");
  const [analysed, setAnalysed] = useState<AnalysedFilter>("all");
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");

  const effectiveFrom = fromDate || bounds?.earliest || "";
  const effectiveTo = toDate || bounds?.latest || "";

  const filtered = useMemo(
    () =>
      articles.filter((article) => {
        const day = toDay(article.published_at);
        if (effectiveFrom && day < effectiveFrom) return false;
        if (effectiveTo && day > effectiveTo) return false;
        if (source !== "all" && article.source !== source) return false;
        if (sentiment !== "all" && article.label !== sentiment) return false;
        if (analysed === "analysed" && !article.has_compatible_analysis) return false;
        if (analysed === "not_analysed" && article.has_compatible_analysis) return false;
        return true;
      }),
    [articles, effectiveFrom, effectiveTo, source, sentiment, analysed],
  );

  return (
    <Pane
      title="Relevant news"
      // The list is complete for the window, so an unfiltered pane states the plain total rather
      // than "N of N". "X of Y" appears only when the reader's own filters are narrowing it.
      meta={
        filtered.length === articles.length
          ? `${articles.length} articles · ${relevantNews.window_days}d window`
          : `${filtered.length} of ${articles.length} articles · ${relevantNews.window_days}d window`
      }
      controls={
        articles.length > 0 ? (
          <>
            <input
              type="date"
              className="ms-filter-control"
              aria-label="From date"
              value={effectiveFrom}
              min={bounds?.earliest}
              max={effectiveTo || bounds?.latest}
              onChange={(event) => setFromDate(event.target.value)}
            />
            <input
              type="date"
              className="ms-filter-control"
              aria-label="To date"
              value={effectiveTo}
              min={effectiveFrom || bounds?.earliest}
              max={bounds?.latest}
              onChange={(event) => setToDate(event.target.value)}
            />
            <select
              className="ms-filter-control"
              aria-label="Source"
              value={source}
              onChange={(event) => setSource(event.target.value)}
            >
              <option value="all">All sources</option>
              {sources.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
            <select
              className="ms-filter-control"
              aria-label="Sentiment"
              value={sentiment}
              onChange={(event) => setSentiment(event.target.value as SentimentFilter)}
            >
              <option value="all">All sentiment</option>
              <option value="positive">Positive</option>
              <option value="negative">Negative</option>
              <option value="neutral">Neutral</option>
            </select>
            <select
              className="ms-filter-control"
              aria-label="Analysed status"
              value={analysed}
              onChange={(event) => setAnalysed(event.target.value as AnalysedFilter)}
            >
              <option value="all">Analysed + not analysed</option>
              <option value="analysed">Analysed only</option>
              <option value="not_analysed">Not analysed only</option>
            </select>
          </>
        ) : undefined
      }
      className="ms-articles-pane"
    >
      {articles.length === 0 ? (
        <p className="ms-empty-note">{relevantNews.empty_message}</p>
      ) : filtered.length === 0 ? (
        <p className="ms-empty-note">No stored articles match these filters.</p>
      ) : (
        <ul className="ms-articles-list">
          {filtered.map((article) => (
            <ArticleRow
              key={article.article_id}
              article={article}
              isSelected={article.article_id === selectedArticleId}
              onSelect={onSelect}
            />
          ))}
        </ul>
      )}
    </Pane>
  );
}

/**
 * An analysed row opens MarketSentinel's own stored analysis in the detail pane; the original
 * publisher article is a separate, explicit external link. A row with no compatible stored
 * analysis stays read-only — there is no action offered to create one.
 */
function ArticleRow({
  article,
  isSelected,
  onSelect,
}: {
  article: ArticleRowView;
  isSelected: boolean;
  onSelect: (articleId: string) => void;
}) {
  const analysed = article.has_compatible_analysis;
  const open = () => analysed && onSelect(article.article_id);

  return (
    <li
      className={`ms-articles-row${analysed ? " ms-articles-row-analysed" : ""}${
        isSelected ? " ms-row-selected" : ""
      }`}
      {...(analysed
        ? {
            tabIndex: 0,
            role: "button",
            "aria-label": `Open the stored analysis of: ${article.title}`,
            onClick: open,
            onKeyDown: (keyEvent: React.KeyboardEvent) => {
              if (keyEvent.key === "Enter" || keyEvent.key === " ") {
                keyEvent.preventDefault();
                open();
              }
            },
          }
        : {})}
    >
      <span className="ms-num-qualifier ms-articles-date">{formatDate(article.published_at)}</span>
      <span className="ms-articles-title" title={article.title}>
        {article.title}
      </span>
      <span className="ms-qualifier ms-articles-source">
        {article.source}
        {article.is_demo && " · demo data"}
      </span>
      <Tag tone={article.label}>{sentimentLabel(article.label)}</Tag>
      <span className="ms-chip">{analysed ? "Analysed" : "Not analysed"}</span>
      <a
        className="ms-articles-original"
        href={article.url}
        target="_blank"
        rel="noreferrer"
        title="Open the original article on the publisher's site"
        onClick={(clickEvent) => clickEvent.stopPropagation()}
      >
        Original ↗
      </a>
    </li>
  );
}
