/**
 * Mock mode for the recall finder and the exports: FICTIONAL recall rows (Acme devices, as in the
 * other example files), the same filters as the API applied here, and a fake file for downloads.
 */
import type { FilteredEntity, RecallPage, RecallRow } from "../api/export";
import { CATEGORY_OPTIONS, CLASS_OPTIONS, STATUS_OPTIONS } from "../api/export";
import type { EntityResponse } from "../api/types";

const CAUSES: [string, string][] = [
  ["Software design", "Software"],
  ["Device Design", "Design"],
  ["Process control", "Manufacturing and process"],
  ["Nonconforming Material/Component", "Component or supplier"],
  ["Error in labeling", "Labeling and packaging"],
  ["Under Investigation by firm", "Other or unknown"],
];
const PRODUCTS = ["AcmeAir CPAP", "Acme Infusion Pump", "Acme GlucoSense", "Acme Legacy Nebulizer"];
const STATUSES = ["Open, Classified", "Completed", "Terminated"];

/** 64 fictional recalls, newest first (more than one page of 50). */
export const MOCK_RECALLS: RecallRow[] = Array.from({ length: 64 }, (_, i) => {
  const [root_cause, category] = CAUSES[i % CAUSES.length]!;
  const year = 2025 - (i % 12);
  const month = String(12 - (i % 12)).padStart(2, "0");
  return {
    recall_number: `Z-${String(1000 + i).padStart(4, "0")}-${year}`,
    recall_class: i % 7 === 6 ? null : `Class ${["I", "II", "III"][i % 3]}`,
    record_date: `${year}-${month}-15`,
    recall_status: STATUSES[i % STATUSES.length]!,
    product: `${PRODUCTS[i % PRODUCTS.length]} (example ${i + 1})`,
    firm: "Example Medical Inc.",
    reason_text: `Example reason ${i + 1}: ${root_cause.toLowerCase()} issue found by the firm.`,
    root_cause,
    action: "Example action: customers were told to contact the firm.",
    product_code: ["ZZZ", "ZZY"][i % 2]!,
    manufacturer_name_norm: "example medical",
    category,
  };
}).sort((a, b) => (a.record_date! < b.record_date! ? 1 : -1));

const label = (options: readonly { value: string; label: string }[], value: string) =>
  options.find((o) => o.value.toLowerCase() === value.toLowerCase())?.label ?? value;

export function describeFilters(filters: URLSearchParams): [string, string][] {
  const lines: [string, string][] = [];
  const all = (k: string) => filters.getAll(k);
  if (all("category").length) lines.push(["Recall category", all("category").map((v) => label(CATEGORY_OPTIONS, v)).join(", ")]);
  if (all("class").length) lines.push(["Recall class", all("class").map((v) => label(CLASS_OPTIONS, v)).join(", ")]);
  if (all("status").length) lines.push(["Recall status", all("status").map((v) => label(STATUS_OPTIONS, v)).join(", ")]);
  if (filters.get("year_from") || filters.get("year_to"))
    lines.push(["Years", `${filters.get("year_from") ?? 1990} to ${filters.get("year_to") ?? "now"}`]);
  if (filters.get("text")) lines.push(["Text contains", filters.get("text")!]);
  if (filters.get("manufacturer")) lines.push(["Manufacturer", filters.get("manufacturer")!]);
  if (filters.get("product_code")) lines.push(["Product code", filters.get("product_code")!]);
  if (all("event").length) lines.push(["Event type", all("event").join(", ")]);
  return lines.length ? lines : [["Filters", "none"]];
}

/** The API's recall filters (export/data.py recall_matches), for example rows. */
export function recallMatches(r: RecallRow, filters: URLSearchParams): boolean {
  const cats = filters.getAll("category").map((v) => label(CATEGORY_OPTIONS, v));
  if (cats.length && !cats.includes(r.category)) return false;
  const classes = filters.getAll("class").map((v) => `Class ${v.toUpperCase()}`);
  if (classes.length && !classes.includes(r.recall_class ?? "")) return false;
  const statuses = filters.getAll("status");
  if (statuses.length) {
    const s = r.recall_status ?? "";
    const ok = statuses.some((v) => (v === "open" ? s.startsWith("Open") : s.toLowerCase() === v));
    if (!ok) return false;
  }
  const date = r.record_date ?? "";
  const from = filters.get("year_from");
  const to = filters.get("year_to");
  if (from && (!date || date < `${from}-01-01`)) return false;
  if (to && (!date || date > `${to}-12-31`)) return false;
  const text = filters.get("text")?.toLowerCase();
  if (text && ![r.product, r.firm, r.reason_text].some((v) => (v ?? "").toLowerCase().includes(text))) return false;
  const maker = filters.get("manufacturer");
  if (maker && r.manufacturer_name_norm !== maker) return false;
  const code = filters.get("product_code");
  if (code && r.product_code !== code.toUpperCase()) return false;
  return true;
}

export function mockRecallPage(filters: URLSearchParams, page: number): RecallPage {
  const rows = MOCK_RECALLS.filter((r) => recallMatches(r, filters));
  const pages = Math.max(1, Math.ceil(rows.length / 50));
  const p = Math.min(Math.max(1, page), pages);
  return {
    total: rows.length,
    page: p,
    pages,
    page_size: 50,
    rows: rows.slice((p - 1) * 50, p * 50),
    filters: describeFilters(filters),
    data_as_of: "2026-11-24",
  };
}

/** A device page with the example recalls and the year and event filters applied. */
export function mockFilteredEntity(page: EntityResponse, filters: URLSearchParams): FilteredEntity {
  const from = Number(filters.get("year_from") ?? 0);
  const to = Number(filters.get("year_to") ?? 9999);
  const events = filters.getAll("event");
  const byYear: Record<string, Record<string, number>> = {};
  for (const [year, byEvent] of Object.entries(page.counts?.by_year ?? {})) {
    if (Number(year) < from || Number(year) > to) continue;
    const kept = Object.fromEntries(Object.entries(byEvent).filter(([e]) => !events.length || events.includes(e)));
    if (Object.keys(kept).length) byYear[year] = kept;
  }
  const byType: Record<string, number> = {};
  for (const byEvent of Object.values(byYear)) for (const [e, n] of Object.entries(byEvent)) byType[e] = (byType[e] ?? 0) + n;
  const recalls = (page.recalls?.shown ?? []).filter((r) =>
    recallMatches({ ...r, product_code: null, manufacturer_name_norm: null, category: "Other or unknown" }, filters),
  );
  return {
    ...page,
    counts: { total_reports: Object.values(byType).reduce((a, b) => a + b, 0), by_event_type: byType, by_year: byYear },
    recalls: { total: recalls.length, shown: recalls },
    filters: describeFilters(filters),
    unfiltered_recalls: page.recalls?.total ?? 0,
  };
}

/** A small placeholder file: mock mode builds no real PDF or Excel file. */
export function mockFile(path: string): Blob {
  const type = path.includes("format=pdf")
    ? "application/pdf"
    : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  return new Blob([`Example file (mock mode, no real data). Requested: ${path}\n`], { type });
}
