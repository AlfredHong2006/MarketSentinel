export function LoadingView({ symbol }: { symbol: string }) {
  return (
    <div className="ms-state-view" role="status" aria-busy="true">
      <p className="ms-state-title">Loading {symbol} overview…</p>
      <p className="ms-qualifier">Reading stored coverage and recomputing materiality and risk.</p>
    </div>
  );
}

export function ErrorView({
  title,
  message,
  onRetry,
}: {
  title: string;
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="ms-state-view" role="alert">
      <p className="ms-state-title">{title}</p>
      <p className="ms-qualifier">{message}</p>
      <button type="button" className="ms-btn ms-btn-secondary" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

export function EmptyOverviewView({ symbol, message }: { symbol: string; message: string }) {
  return (
    <div className="ms-state-view">
      <p className="ms-state-title">No coverage yet for {symbol}</p>
      <p className="ms-qualifier">{message}</p>
    </div>
  );
}
