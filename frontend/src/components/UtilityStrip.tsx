import { formatDateTime } from "../format";

export function UtilityStrip({
  articlesIndexed,
  materialCount,
  generatedAt,
  horizon,
}: {
  articlesIndexed: number;
  materialCount: number;
  generatedAt: string;
  horizon: string;
}) {
  return (
    <div className="ms-utility-strip">
      <span>
        {articlesIndexed} items indexed · {materialCount} material
      </span>
      <span>Coverage refreshed {formatDateTime(generatedAt)}</span>
      <span className="ms-utility-spacer" />
      <span>Horizon {horizon} · basis: reported</span>
    </div>
  );
}
