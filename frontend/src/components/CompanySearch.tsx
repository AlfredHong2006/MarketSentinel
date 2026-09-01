import { useEffect, useRef, useState } from "react";
import { searchConstituents } from "../api/client";
import type { CapabilitiesView, Constituent, MarketName } from "../api/types";

const MARKETS: (MarketName | "All")[] = ["All", "S&P 500", "FTSE 100"];
const DEBOUNCE_MS = 300;

/**
 * Company/ticker search — the read-only replacement for the Streamlit sidebar's constituent
 * picker. Uses GET /api/v1/constituents/search only; never triggers an analysis.
 *
 * Results and their order come from the server untouched. `capabilities` adds coverage metadata
 * only: which companies are the prepared deep demos, and how many articles each ticker has
 * stored — server-reported facts, never a client-derived quality judgement or filter.
 */
function coverageLabel(symbol: string, capabilities: CapabilitiesView | null): string | null {
  if (!capabilities) return null;
  const count = capabilities.coverage[symbol] ?? 0;
  if (capabilities.prepared_companies.includes(symbol)) {
    return `Prepared coverage · ${count} articles`;
  }
  if (count > 0) return `${count} stored articles`;
  return "No stored coverage";
}

export function CompanySearch({
  currentSymbol,
  onSelect,
  capabilities,
}: {
  currentSymbol: string;
  onSelect: (symbol: string) => void;
  capabilities: CapabilitiesView | null;
}) {
  const [query, setQuery] = useState("");
  const [market, setMarket] = useState<(typeof MARKETS)[number]>("All");
  const [results, setResults] = useState<Constituent[]>([]);
  const [fallbackMessage, setFallbackMessage] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const requestSeq = useRef(0);

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      setFallbackMessage(null);
      setErrorMessage(null);
      return;
    }
    const seq = ++requestSeq.current;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      searchConstituents(query.trim(), market, controller.signal)
        .then((data) => {
          if (requestSeq.current !== seq) return;
          setResults(data.constituents);
          setFallbackMessage(data.is_fallback ? (data.message ?? "A fallback universe is in use.") : null);
          setErrorMessage(null);
        })
        .catch((error: unknown) => {
          if (error instanceof DOMException && error.name === "AbortError") return;
          if (requestSeq.current !== seq) return;
          setResults([]);
          setErrorMessage(error instanceof Error ? error.message : "Search failed.");
        });
    }, DEBOUNCE_MS);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [query, market]);

  return (
    <nav className="ms-rail" aria-label="Find a company">
      <div className="ms-rail-group-label">Find a company</div>
      <div className="ms-rail-search">
        <select
          className="ms-rail-select"
          value={market}
          onChange={(event) => setMarket(event.target.value as (typeof MARKETS)[number])}
          aria-label="Index"
        >
          {MARKETS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <input
          type="text"
          className="ms-rail-input"
          placeholder="Company or ticker"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label="Company or ticker"
        />
      </div>
      {errorMessage && <p className="ms-rail-note ms-rail-note-error">{errorMessage}</p>}
      {fallbackMessage && <p className="ms-rail-note">{fallbackMessage}</p>}
      {query.trim() && (
        <ul className="ms-rail-results">
          {results.length === 0 && !errorMessage && <li className="ms-rail-note">No matches.</li>}
          {results.map((item) => (
            <li key={item.symbol}>
              <button
                type="button"
                className={`ms-rail-result${item.symbol === currentSymbol ? " ms-rail-result-active" : ""}`}
                onClick={() => onSelect(item.symbol)}
              >
                <span className="ms-rail-result-symbol">{item.symbol}</span>
                <span className="ms-rail-result-name">{item.name}</span>
                <span className="ms-rail-result-market">{item.market}</span>
                {coverageLabel(item.symbol, capabilities) && (
                  <span className="ms-rail-result-coverage">
                    {coverageLabel(item.symbol, capabilities)}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="ms-rail-current">
        <div className="ms-rail-group-label">Current</div>
        <div className="ms-rail-current-symbol">{currentSymbol}</div>
      </div>
    </nav>
  );
}
