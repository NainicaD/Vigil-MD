import type { Counts } from "../../api/types";
import { formatNumber, formatReports } from "../../lib/format";

/**
 * Total reports and reports by event type, with the API's counts_note right next to the
 * numbers (handover §4). Event types are listed in the order the API sends them.
 */
export function ReportCounts({ counts, note }: { counts: Counts; note: string | null }) {
  const byType = Object.entries(counts.by_event_type ?? {});
  return (
    <section className="card" aria-labelledby="reports-heading">
      <h2 id="reports-heading">Reports</h2>
      <p className="big-number">{formatReports(counts.total_reports ?? 0)}</p>
      {note && <p className="counts-note">{note}</p>}
      {byType.length > 0 && (
        <>
          <h3>Reports by event type</h3>
          <dl className="kv">
            {byType.map(([type, n]) => (
              <div key={type} className="kv-row">
                <dt>{type}</dt>
                <dd>{formatReports(n)}</dd>
              </div>
            ))}
          </dl>
        </>
      )}
      {counts.total_reports > 0 && byType.length === 0 && (
        <p className="muted">No breakdown by event type was given. Total: {formatNumber(counts.total_reports)}.</p>
      )}
    </section>
  );
}
