/**
 * Presentation-only formatting of values the API already supplies. Nothing here derives a
 * verdict, threshold, ranking, or label the backend owns — those belong to the deterministic
 * server-side layers. See docs/architecture/ARCHITECTURE.md and AGENTS.md "Do not invent
 * consequential decisions".
 */

// "28 Aug 2026" — day, abbreviated month, year. Matches the design system's date convention
// (never a numeric-only format).
const DATE_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
});

export function formatDate(iso: string): string {
  return DATE_FORMATTER.format(new Date(iso));
}

export function formatDateTime(iso: string): string {
  return DATE_TIME_FORMATTER.format(new Date(iso)).replace(",", ",");
}

export function formatPrice(value: number): string {
  return value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatPercent(value: number, digits = 0): string {
  return `${(value * 100).toFixed(digits)}%`;
}

const DIRECTION_LABEL: Record<string, string> = {
  positive: "Supportive",
  negative: "Adverse",
  mixed: "Mixed",
  neutral: "Neutral",
  uncertain: "Uncertain",
};

export function directionLabel(direction: string): string {
  return DIRECTION_LABEL[direction] ?? direction;
}

const EVENT_TYPE_LABEL: Record<string, string> = {
  earnings: "Earnings",
  product_launch: "Product launch",
  investment: "Investment",
  acquisition: "Acquisition",
  regulation: "Regulation",
  litigation: "Litigation",
  supply_disruption: "Supply disruption",
  management_change: "Management change",
  financing: "Financing",
  macroeconomic_exposure: "Macroeconomic exposure",
  partnership: "Partnership",
  contract_award: "Contract award",
  contract_loss: "Contract loss",
  analyst_or_guidance_change: "Analyst or guidance change",
  other: "Other",
  uncertain: "Uncertain",
};

export function eventTypeLabel(eventType: string): string {
  return EVENT_TYPE_LABEL[eventType] ?? eventType;
}

const HORIZON_LABEL: Record<string, string> = {
  immediate: "Immediate",
  days: "Days",
  weeks: "Weeks",
  months: "Months",
  long_term: "Long term",
  uncertain: "Uncertain",
};

export function horizonLabel(horizon: string): string {
  return HORIZON_LABEL[horizon] ?? horizon;
}

const EVIDENCE_STATUS_LABEL: Record<string, string> = {
  corroborated: "Corroborated",
  contradicted: "Contradicted",
  unsupported: "Unsupported",
  uncertain: "Uncertain",
};

export function evidenceStatusLabel(status: string): string {
  return EVIDENCE_STATUS_LABEL[status] ?? status;
}

const SENTIMENT_LABEL: Record<string, string> = {
  positive: "Positive",
  negative: "Negative",
  neutral: "Neutral",
};

export function sentimentLabel(label: string): string {
  return SENTIMENT_LABEL[label] ?? label;
}
