/** Formats for the developer page. */

export const formatMs = (ms: number | null | undefined) => (ms === null || ms === undefined ? "—" : `${ms.toLocaleString("en-US")} ms`);

/** BigQuery cost, as billed by the API (GB, rounded to 4 places by the API). */
export const formatGb = (gb: number | null | undefined) => (gb === null || gb === undefined ? "—" : `${gb.toFixed(4)} GB`);

/** "2026-11-11 19:22:52" (local time), or "—". */
export function formatTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** "19:22:52" (local time). */
export function formatClock(ms: number): string {
  return formatTime(ms).slice(11);
}
