"use client";

import { clearCalls, summarize, useCallLog } from "../../dev/callLog";
import { formatClock, formatGb, formatMs } from "../../dev/format";

/** Every API call made in this tab (search, device pages, assistant, this page), newest first. */
export function SessionLog() {
  const calls = useCallLog();
  const s = summarize(calls);

  return (
    <section className="dev-section" aria-labelledby="dev-log">
      <h2 id="dev-log">Session log</h2>
      <p className="hint">
        Every call this browser tab made to the API, from any page. Kept in memory only: it is gone when the page is
        reloaded, and never sent anywhere.
      </p>
      <dl className="dev-facts dev-summary">
        <dt>Calls</dt>
        <dd>{s.total}</dd>
        <dt>Failed</dt>
        <dd>{s.failed}</dd>
        <dt>Cancelled</dt>
        <dd>{s.cancelled}</dd>
        <dt>Average time</dt>
        <dd>{formatMs(s.avgMs)}</dd>
        <dt>Slowest</dt>
        <dd>{formatMs(s.maxMs)}</dd>
        <dt>BigQuery cost</dt>
        <dd>{formatGb(s.totalGb)}</dd>
      </dl>

      {calls.length === 0 ? (
        <p className="muted">No calls yet. Search, open a device or ask a question; the calls appear here.</p>
      ) : (
        <>
          <div className="dev-table-wrap">
            <table className="dev-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Call</th>
                  <th>Asked</th>
                  <th>Result</th>
                  <th>Took</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                {calls.map((c) => (
                  <tr key={c.id} className={`log-${c.outcome}`}>
                    <td>{formatClock(c.at)}</td>
                    <td>{c.call}</td>
                    <td>{c.detail || "—"}</td>
                    <td>{c.outcome === "failed" ? <strong>{c.result}</strong> : c.result}</td>
                    <td>{formatMs(c.ms)}</td>
                    <td>{c.gb === null ? "—" : formatGb(c.gb)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button type="button" className="btn secondary" onClick={clearCalls}>
            Clear log
          </button>
        </>
      )}
    </section>
  );
}
