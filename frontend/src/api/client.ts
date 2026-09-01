import type {
  CapabilitiesView,
  CompanyOverview,
  RelevantNewsView,
  StoredArticleAnalysisView,
  UniverseResult,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export class ApiNotFoundError extends Error {}
export class ApiServerError extends Error {}
export class ApiNetworkError extends Error {}

/**
 * Every call in this module is a GET. This app has no write client at all: it never triggers
 * ingestion, sentiment scoring, a coverage refresh, or a paid per-article analysis. The two
 * endpoints that spend money (POST /api/v1/analyze, POST /api/v1/articles/analyze) have no
 * counterpart here, and a public deployment additionally refuses them at the API boundary.
 */
async function getJson<T>(path: string, notFoundMessage: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { method: "GET", signal });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw cause;
    }
    throw new ApiNetworkError(`Could not reach the MarketSentinel API at ${API_BASE_URL}.`);
  }

  if (response.status === 404) {
    const body = await safeJson(response);
    throw new ApiNotFoundError(
      typeof body?.detail === "string" ? body.detail : notFoundMessage,
    );
  }

  if (!response.ok) {
    const body = await safeJson(response);
    throw new ApiServerError(
      typeof body?.detail === "string"
        ? body.detail
        : `The MarketSentinel API returned an unexpected error (${response.status}).`,
    );
  }

  return (await response.json()) as T;
}

/**
 * Reads what this deployment exposes — public or private, the default company, and the covered
 * set. Advisory only: the server independently enforces every restriction reported here, so
 * this response widens no access.
 */
export function fetchCapabilities(signal?: AbortSignal): Promise<CapabilitiesView> {
  return getJson<CapabilitiesView>(
    "/api/v1/capabilities",
    "Capabilities were not found.",
    signal,
  );
}

/** Reads one company's Company Overview. Matches GET /api/v1/companies/{symbol}/overview. */
export function fetchCompanyOverview(
  symbol: string,
  signal?: AbortSignal,
): Promise<CompanyOverview> {
  return getJson<CompanyOverview>(
    `/api/v1/companies/${encodeURIComponent(symbol)}/overview`,
    `${symbol} was not found.`,
    signal,
  );
}

/**
 * Reads one company's stored, sentiment-scored articles — the read-only Relevant News browser.
 * Matches GET /api/v1/companies/{symbol}/articles.
 */
export function fetchRelevantNews(
  symbol: string,
  signal?: AbortSignal,
): Promise<RelevantNewsView> {
  return getJson<RelevantNewsView>(
    `/api/v1/companies/${encodeURIComponent(symbol)}/articles`,
    `${symbol} was not found.`,
    signal,
  );
}

/**
 * Reads one article's already-stored analysis. Strictly a read of what exists — a 404 means no
 * compatible stored analysis, never an invitation to generate one.
 */
export function fetchArticleAnalysis(
  symbol: string,
  articleId: string,
  signal?: AbortSignal,
): Promise<StoredArticleAnalysisView> {
  return getJson<StoredArticleAnalysisView>(
    `/api/v1/companies/${encodeURIComponent(symbol)}/articles/${encodeURIComponent(articleId)}/analysis`,
    "No stored analysis was found for this article.",
    signal,
  );
}

/**
 * Reads the constituent search results. Matches GET /api/v1/constituents/search — the same
 * lookup the Streamlit sidebar used. A public deployment serves only its covered companies here,
 * so the client applies no allowlist of its own.
 */
export function searchConstituents(
  query: string,
  market: string,
  signal?: AbortSignal,
): Promise<UniverseResult> {
  const params = new URLSearchParams({ q: query, market, limit: "30" });
  return getJson<UniverseResult>(
    `/api/v1/constituents/search?${params}`,
    "Constituent search was not found.",
    signal,
  );
}

async function safeJson(response: Response): Promise<{ detail?: unknown } | null> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}
