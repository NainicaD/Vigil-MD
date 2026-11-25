import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { filterParams, type RecallRow } from "../api/export";
import { DownloadButtons } from "../components/export/DownloadButtons";
import { FilterBar } from "../components/export/FilterBar";
import "../components/export/export.css";
import { useRecalls } from "../hooks/useRecalls";
import { formatNumber } from "../lib/format";

/** Reasons are cut in the table; the files hold them in full (Excel) or to about 300 characters (PDF). */
const REASON_CHARS = 200;

function cut(text: string | null, max: number): string {
  if (!text) return "";
  if (text.length <= max) return text;
  const part = text.slice(0, max);
  return `${part.slice(0, part.lastIndexOf(" ") > max / 2 ? part.lastIndexOf(" ") : max).trimEnd()}…`;
}

/** The recall finder at /recalls: every recall across all devices, filtered, 50 per page. */
export function RecallsPage() {
  const [params, setParams] = useSearchParams();
  const filters = filterParams(params, "recalls");
  const page = Math.max(1, Number.parseInt(params.get("page") ?? "1", 10) || 1);
  const { state, retry } = useRecalls(filters.toString(), page);
  const data = state.status === "done" ? state.data : state.status === "loading" ? state.previous : undefined;

  useEffect(() => {
    document.title = "Recalls · Vigil MD";
    return () => {
      document.title = "Vigil MD";
    };
  }, []);

  const goTo = (p: number) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (p > 1) next.set("page", String(p));
      else next.delete("page");
      return next;
    });

  return (
    <div className="page">
      <h1>Recalls</h1>
      <p className="lead">
        Every FDA medical device recall. Narrow the list with the filters, then download exactly what you see.
      </p>

      <FilterBar scope="recalls" />

      <div className="search-state" aria-live="polite">
        {state.status === "loading" && !data && <p className="hint">Loading…</p>}
        {state.status === "loading" && data && <p className="hint">Updating…</p>}
        {state.status === "error" && (
          <div className="notice" role="alert">
            <p>{state.message}</p>
            <button type="button" className="btn" onClick={retry}>
              Try again
            </button>
          </div>
        )}
      </div>

      {data && (
        <div className={state.status === "loading" ? "updating" : undefined}>
          <DownloadButtons target={{ kind: "recalls" }} filters={filters} count={data.total} />
          {data.data_as_of && <p className="muted">Data as of {data.data_as_of}.</p>}
          {data.total === 0 ? (
            <p className="notice">No recalls match these filters.</p>
          ) : (
            <>
              <RecallTable rows={data.rows} />
              <nav className="pager" aria-label="Pages">
                <button type="button" className="btn secondary small" disabled={data.page <= 1} onClick={() => goTo(data.page - 1)}>
                  Previous
                </button>
                <span>
                  Page {formatNumber(data.page)} of {formatNumber(data.pages)} ({formatNumber(data.total)} recalls)
                </span>
                <button
                  type="button"
                  className="btn secondary small"
                  disabled={data.page >= data.pages}
                  onClick={() => goTo(data.page + 1)}
                >
                  Next
                </button>
              </nav>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function RecallTable({ rows }: { rows: RecallRow[] }) {
  return (
    <div className="recall-table-wrap">
      <table className="recall-table">
        <caption className="visually-hidden">Recalls, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Class</th>
            <th scope="col">Status</th>
            <th scope="col">Category</th>
            <th scope="col">Firm</th>
            <th scope="col">Product</th>
            <th scope="col">Reason</th>
            <th scope="col">Recall number</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.recall_number}-${i}`}>
              <td className="nowrap">{r.record_date?.slice(0, 10) ?? "Not given"}</td>
              <td className="nowrap">{r.recall_class ?? "Not given"}</td>
              <td>{r.recall_status ?? "Not given"}</td>
              <td>{r.category}</td>
              <td>{r.firm ?? ""}</td>
              <td>{r.product ?? ""}</td>
              <td>{cut(r.reason_text, REASON_CHARS)}</td>
              <td className="nowrap">{r.recall_number ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
