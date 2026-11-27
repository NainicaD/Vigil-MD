import { Link } from "react-router-dom";
import { ENTITY_KINDS, type EntityKind, type SearchHit } from "../api/types";
import { KIND_GROUP_LABEL, KIND_LABEL, formatNumber, formatReports } from "../lib/format";
import { paths } from "../routes";

export const NO_RESULTS_MESSAGE = "Nothing found. Try the brand name, the company or the 3-letter product code.";
/** What each source label on a result means. */
export const SOURCES_LEGEND =
  "MAUDE: FDA adverse-event reports · Recalls: FDA device recalls · Enforcement: FDA enforcement reports";

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
      <p className="hint sources-legend">{SOURCES_LEGEND}</p>
    </div>
  );
}

function HitItem({ hit }: { hit: SearchHit }) {
  const reports = formatReports(hit.reports);
  const kind = KIND_LABEL[hit.kind as EntityKind] ?? hit.kind;
  const hasSources = typeof hit.recalls === "number" || typeof hit.enforcement === "number";
  return (
    <li>
      <Link className="hit" to={paths.device(hit.kind, hit.entity_id)}>
        <span className="tag">{kind}</span>
        <span className="hit-title">{hit.title}</span>
        {hit.subtitle && <span className="hit-subtitle">{hit.subtitle}</span>}
        {(reports || hasSources) && (
          <span className="hit-reports hit-sources">
            {reports && <Source name="MAUDE" value={reports} />}
            {hasSources && <Source name="Recalls" value={formatNumber(hit.recalls ?? 0)} />}
            {hasSources && <Source name="Enforcement" value={formatNumber(hit.enforcement ?? 0)} />}
          </span>
        )}
      </Link>
    </li>
  );
}

/** One source's count on a result, labelled with the source ("MAUDE 1,234 reports"). */
function Source({ name, value }: { name: string; value: string }) {
  return (
    <span className="source">
      <span className="source-name">{name}</span> {value}
    </span>
  );
}
