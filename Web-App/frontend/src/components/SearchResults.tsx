import Link from "next/link";
import { ENTITY_KINDS, type EntityKind, type SearchHit } from "../api/types";
import { KIND_GROUP_LABEL, KIND_LABEL, formatReports } from "../lib/format";
import { paths } from "../routes";

export const NO_RESULTS_MESSAGE = "Nothing found. Try the brand name, the company or the 3-letter product code.";

/** Results grouped by kind (devices, companies, product codes), each in the API's order. */
export function SearchResults({ hits }: { hits: SearchHit[] }) {
  if (hits.length === 0) {
    return (
      <p className="notice" role="status">
        {NO_RESULTS_MESSAGE}
      </p>
    );
  }
  const groups = ENTITY_KINDS.map((kind) => ({ kind, hits: hits.filter((h) => h.kind === kind) })).filter(
    (g) => g.hits.length > 0,
  );
  // Hits of a kind this version doesn't know still show, at the end.
  const other = hits.filter((h) => !ENTITY_KINDS.includes(h.kind));

  return (
    <div className="results">
      {groups.map((g) => (
        <section key={g.kind} aria-labelledby={`results-${g.kind}`}>
          <h2 id={`results-${g.kind}`} className="results-heading">
            {KIND_GROUP_LABEL[g.kind]} <span className="muted">({g.hits.length})</span>
          </h2>
          <ul className="hit-list">
            {g.hits.map((h) => (
              <HitItem key={h.id} hit={h} />
            ))}
          </ul>
        </section>
      ))}
      {other.length > 0 && (
        <ul className="hit-list">
          {other.map((h) => (
            <HitItem key={h.id} hit={h} />
          ))}
        </ul>
      )}
    </div>
  );
}

function HitItem({ hit }: { hit: SearchHit }) {
  const reports = formatReports(hit.reports);
  const kind = KIND_LABEL[hit.kind as EntityKind] ?? hit.kind;
  return (
    <li>
      <Link className="hit" href={paths.device(hit.kind, hit.entity_id)}>
        <span className="tag">{kind}</span>
        <span className="hit-title">{hit.title}</span>
        {hit.subtitle && <span className="hit-subtitle">{hit.subtitle}</span>}
        {reports && <span className="hit-reports">{reports}</span>}
      </Link>
    </li>
  );
}
