import type { ReactNode } from "react";

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

/**
 * The zero-coverage page. `title` and `message` are deployment-state copy the client owns (never
 * an intelligence conclusion); `action` is the optional shared-request control, and `note` is
 * the outcome of the reader's own request (queued, refused, failed).
 */
export function EmptyOverviewView({
  title,
  message,
  action,
  note,
}: {
  title: string;
  message: string;
  action?: ReactNode;
  note?: string | null;
}) {
  return (
    <div className="ms-state-view">
      <p className="ms-state-title">{title}</p>
      <p className="ms-qualifier">{message}</p>
      {action && <div className="ms-state-actions">{action}</div>}
      {note && (
        <p className="ms-qualifier" role="status">
          {note}
        </p>
      )}
    </div>
  );
}
