import type { ReactNode } from "react";

/** Small uppercase tone label. Direction/tier badges — never the sole carrier of meaning. */
export function Tag({ tone, children }: { tone: "positive" | "negative" | "neutral"; children: ReactNode }) {
  return (
    <span className={`ms-tag ms-tag-${tone}`}>{children}</span>
  );
}

/**
 * Hueless evidence-strength meter: ink density only, never a color/sentiment carrier.
 * `level` is the server's own 0-1 evidence_strength float (breadth of supplied context,
 * not a probability) — rendered as a proportional fill, not a re-derived bucket scale.
 */
export function EvidenceStrength({ level, label }: { level: number; label: string }) {
  const clamped = Math.max(0, Math.min(1, level));
  return (
    <span className="ms-evidence" aria-label={`Evidence strength: ${label}`}>
      <span className="ms-evidence-track">
        <span className="ms-evidence-fill" style={{ width: `${clamped * 100}%` }} />
      </span>
      <span className="ms-evidence-label">{label}</span>
    </span>
  );
}

/** Thin track + marker at concern_index/100, coloured by the server-supplied band color. */
export function RiskMarker({
  value,
  color,
  width = 88,
}: {
  value: number;
  color: string;
  width?: number;
}) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <span
      className="ms-risk-marker"
      style={{ width: `${width}px` }}
      role="img"
      aria-label={`Concern index ${value} of 100`}
    >
      <span className="ms-risk-track" />
      <span
        className="ms-risk-dot"
        style={{ left: `${clamped}%`, background: color, borderColor: color }}
      />
    </span>
  );
}

/** One cited source: publisher, date, and a real link — never a fabricated locator. */
export function SourceRef({
  publisher,
  date,
  url,
  title,
}: {
  publisher: string;
  date: string;
  url: string;
  title?: string;
}) {
  return (
    <a className="ms-source-ref" href={url} target="_blank" rel="noreferrer" title={title}>
      <span className="ms-source-publisher">{publisher}</span>
      <span className="ms-source-date">{date}</span>
    </a>
  );
}
