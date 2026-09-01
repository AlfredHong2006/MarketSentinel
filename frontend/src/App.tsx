import { useCallback, useEffect, useState } from "react";
import { fetchCapabilities } from "./api/client";
import type { CapabilitiesView } from "./api/types";
import "./app.css";
import { CompanyOverviewPage } from "./CompanyOverviewPage";

const FALLBACK_SYMBOL = "NVDA";

function symbolFromLocation(): string | null {
  const fromQuery = new URLSearchParams(window.location.search).get("symbol");
  const trimmed = fromQuery?.trim();
  return trimmed ? trimmed.toUpperCase() : null;
}

export default function App() {
  const [capabilities, setCapabilities] = useState<CapabilitiesView | null>(null);
  // Only an explicit ?symbol wins immediately. Otherwise the default comes from the server's
  // capabilities, so which company a deployment opens on is configuration, not a hardcoded
  // client constant.
  const [symbol, setSymbolState] = useState<string | null>(symbolFromLocation);

  useEffect(() => {
    const controller = new AbortController();
    fetchCapabilities(controller.signal)
      .then((result) => {
        setCapabilities(result);
        setSymbolState((current) => current ?? result.default_symbol);
      })
      .catch(() => setSymbolState((current) => current ?? FALLBACK_SYMBOL));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const onPopState = () => setSymbolState(symbolFromLocation() ?? FALLBACK_SYMBOL);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const setSymbol = useCallback((next: string) => {
    const normalised = next.trim().toUpperCase();
    setSymbolState((current) => {
      if (normalised === current) return current;
      const url = new URL(window.location.href);
      url.searchParams.set("symbol", normalised);
      window.history.pushState({}, "", url);
      return normalised;
    });
  }, []);

  if (symbol === null) return null;

  return (
    <CompanyOverviewPage
      symbol={symbol}
      onSymbolChange={setSymbol}
      capabilities={capabilities}
    />
  );
}
