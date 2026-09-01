import type { DataSource } from "../api/types";

/** Fixed 40px dark app header — the only dark region in light mode. Carries identity only. */
export function AppHeader({
  articlesIndexed,
  dataSource,
}: {
  articlesIndexed: number | null;
  dataSource: DataSource | null;
}) {
  return (
    <header className="ms-app-header">
      <div className="ms-wordmark">MarketSentinel</div>
      {articlesIndexed !== null && (
        <span className="ms-header-status">
          {dataSource && (
            <span
              className={`ms-header-status-dot${dataSource === "refreshed" ? " ms-header-status-dot-fresh" : ""}`}
            />
          )}
          {dataSource === "refreshed" ? "Recomputed now" : dataSource === "stored" ? "From stored analysis" : "Coverage"}
          {" · "}
          {articlesIndexed} items indexed
        </span>
      )}
    </header>
  );
}
