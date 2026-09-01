import type { Constituent } from "../api/types";
import { formatDate } from "../format";

export function IdentityHeader({
  constituent,
  generatedAt,
}: {
  constituent: Constituent;
  generatedAt: string;
}) {
  return (
    <div className="ms-identity">
      <div className="ms-identity-row">
        <h1 className="ms-identity-name">{constituent.name}</h1>
        <span className="ms-identity-symbol">{constituent.symbol}</span>
        <span className="ms-identity-market">
          {constituent.market} · as of {formatDate(generatedAt)}
        </span>
      </div>
    </div>
  );
}
