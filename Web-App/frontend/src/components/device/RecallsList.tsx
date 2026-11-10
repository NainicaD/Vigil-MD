import type { Recall, Recalls } from "../../api/types";
import { formatNumber } from "../../lib/format";

const NOT_GIVEN = "Not given";

/**
 * Recalls in the order the API sends them (newest first), each with class, date, status and
 * reason. Classes are plain text: no colours that could read as danger (handover §4).
 */
export function RecallsList({ recalls }: { recalls: Recalls }) {
  const shown = recalls.shown ?? [];
  const total = recalls.total ?? shown.length;

  return (
    <section className="card" aria-labelledby="recalls-heading">
      <h2 id="recalls-heading">Recalls</h2>
      {total === 0 ? (
        <p className="muted">No recalls found.</p>
      ) : (
        <>
          <p>
            {formatNumber(total)} {total === 1 ? "recall" : "recalls"} found
            {shown.length < total ? `. Showing the newest ${formatNumber(shown.length)}.` : "."}
          </p>
          <ol className="recalls">
            {shown.map((r, i) => (
              <RecallItem key={`${r.recall_number ?? "recall"}-${i}`} recall={r} />
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

function RecallItem({ recall: r }: { recall: Recall }) {
  const extra = [
    ["Recall number", r.recall_number],
    ["Product", r.product],
    ["Firm", r.firm],
    ["Root cause", r.root_cause],
    ["Action", r.action],
  ].filter(([, v]) => v) as [string, string][];

  return (
    <li className="recall">
      <dl className="recall-facts">
        <div>
          <dt>Class</dt>
          <dd>{r.recall_class ?? NOT_GIVEN}</dd>
        </div>
        <div>
          <dt>Date</dt>
          <dd>{r.record_date ? r.record_date.slice(0, 10) : NOT_GIVEN}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{r.recall_status ?? NOT_GIVEN}</dd>
        </div>
      </dl>
      <p className="recall-reason">
        <span className="label-inline">Reason: </span>
        {r.reason_text ?? NOT_GIVEN}
      </p>
      {extra.length > 0 && (
        <details className="more">
          <summary>More details</summary>
          <dl className="kv">
            {extra.map(([k, v]) => (
              <div key={k} className="kv-row">
                <dt>{k}</dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </li>
  );
}
