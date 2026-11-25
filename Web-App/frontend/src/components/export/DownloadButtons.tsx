import { useState } from "react";
import { failureMessage } from "../../api/errors";
import { PDF_MAX, XLSX_MAX, downloadExport, type ExportFormat, type ExportTarget } from "../../api/export";
import { formatNumber } from "../../lib/format";
import "./export.css";

/**
 * "Download PDF" and "Download Excel" for what the page shows: the same target and the same
 * filters (read from the current address by the page). Shows the row count first; over the PDF
 * limit the PDF button is off and says why (Excel stays available). Errors from the API (413 too
 * many rows, 429 too many downloads, 503) are shown as the API words them.
 */
export function DownloadButtons({
  target,
  filters,
  count,
  noun = "recalls",
}: {
  target: ExportTarget;
  filters?: URLSearchParams;
  /** Rows the file will hold, when known (recall finder, device page). */
  count?: number;
  noun?: string;
}) {
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const overPdf = count !== undefined && count > PDF_MAX;
  const overExcel = count !== undefined && count > XLSX_MAX;
  const empty = count === 0;

  async function get(format: ExportFormat) {
    setBusy(format);
    setMessage(null);
    try {
      await downloadExport(target, format, filters);
    } catch (error) {
      setMessage(failureMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="downloads">
      {count !== undefined && (
        <p className="download-count">
          {formatNumber(count)} {count === 1 ? noun.replace(/s$/, "") : noun} match
        </p>
      )}
      <div className="download-buttons">
        <button type="button" className="btn" disabled={busy !== null || overPdf || empty} onClick={() => get("pdf")}>
          Download PDF
        </button>
        <button type="button" className="btn" disabled={busy !== null || overExcel || empty} onClick={() => get("xlsx")}>
          Download Excel
        </button>
      </div>
      {overPdf && !overExcel && (
        <p className="hint">
          A PDF holds at most {formatNumber(PDF_MAX)} {noun}. Narrow the filters for a PDF, or download Excel.
        </p>
      )}
      {overExcel && (
        <p className="hint">
          A file holds at most {formatNumber(XLSX_MAX)} {noun}. Narrow the filters to download.
        </p>
      )}
      <p className="download-status" role="status" aria-live="polite">
        {busy ? "Preparing file…" : message}
      </p>
    </div>
  );
}
