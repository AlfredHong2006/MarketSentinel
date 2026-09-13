import type {
  ArticleAnalysisRequestView,
  CapabilitiesView,
  CompanyOverview,
  CoverageRequestView,
  RelevantNewsView,
  StoredArticleAnalysisView,
  UniverseResult,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export class ApiNotFoundError extends Error {}
export class ApiServerError extends Error {}
export class ApiNetworkError extends Error {}
/** 409 or 429: the request was understood but refused by a cap, a limit, or its current state. */
export class ApiRefusedError extends Error {}

/**
 * Reads are GETs. The only writes this client makes are the two *request* POSTs at the bottom of
 * this module, which record a shared, anonymous request for the private scheduled worker and
 * spend nothing themselves. The endpoints that spend money directly (POST /api/v1/analyze,
 * POST /api/v1/articles/analyze) still have no counterpart here, and a public deployment refuses
 * them at the API boundary regardless.
 */
async function requestJson<T>(
  method: "GET" | "POST",
  path: string,
  notFoundMessage: string,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { method, signal });
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

  if (response.status === 409 || response.status === 429) {
    const body = await safeJson(response);
    throw new ApiRefusedError(
      typeof body?.detail === "string" ? body.detail : "The request was not accepted.",
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

function getJson<T>(path: string, notFoundMessage: string, signal?: AbortSignal): Promise<T> {
  return requestJson<T>("GET", path, notFoundMessage, signal);
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

/**
 * Asks for shared coverage of one company. Matches POST
 * /api/v1/companies/{symbol}/coverage-requests. Records a request only: the private scheduled
 * worker starts coverage on a later run, and the result is then visible to everyone.
 */
export function requestCoverage(symbol: string): Promise<CoverageRequestView> {
  return requestJson<CoverageRequestView>(
    "POST",
    `/api/v1/companies/${encodeURIComponent(symbol)}/coverage-requests`,
    "Coverage requests are not available on this deployment.",
  );
}

/**
 * Asks for the analysis of one stored article. Matches POST
 * /api/v1/companies/{symbol}/articles/{article_id}/analysis-requests. Likewise a request only.
 */
export function requestArticleAnalysis(
  symbol: string,
  articleId: string,
): Promise<ArticleAnalysisRequestView> {
  return requestJson<ArticleAnalysisRequestView>(
    "POST",
    `/api/v1/companies/${encodeURIComponent(symbol)}/articles/${encodeURIComponent(articleId)}/analysis-requests`,
    "Analysis requests are not available on this deployment.",
  );
}

async function safeJson(response: Response): Promise<{ detail?: unknown } | null> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}
