/**
 * Filters, export addresses and downloads for the recall finder, the device page and the search
 * list. The filters live in the address bar (shareable links); the table and the file always
 * use the same ones, read from the current address.
 *
 * The values match the API's allow-lists (Model-Development/chatbot/export/filters.py); the
 * server checks them again and answers 422 for anything else.
 */
import { api } from "./client";
import type { EntityKind, EntityResponse, Recall } from "./types";

export type FilterScope = "recalls" | "entity";
export type ExportFormat = "pdf" | "xlsx";

export const CATEGORY_OPTIONS = [
  { value: "software", label: "Software" },
  { value: "design", label: "Design" },
  { value: "manufacturing", label: "Manufacturing and process" },
  { value: "component", label: "Component or supplier" },
  { value: "labeling", label: "Labeling and packaging" },
  { value: "other", label: "Other or unknown" },
] as const;
export const CLASS_OPTIONS = [
  { value: "I", label: "Class I" },
  { value: "II", label: "Class II" },
  { value: "III", label: "Class III" },
] as const;
export const STATUS_OPTIONS = [
  { value: "open", label: "Open (ongoing)" },
  { value: "completed", label: "Completed" },
  { value: "terminated", label: "Terminated" },
] as const;
export const EVENT_OPTIONS = [
  { value: "Death", label: "Death" },
  { value: "Injury", label: "Injury" },
  { value: "Malfunction", label: "Malfunction" },
] as const;

export const MIN_YEAR = 1990;
export const TEXT_MIN = 2;
export const TEXT_MAX = 100;
/** Rows a file may hold (the API answers 413 above them). */
export const PDF_MAX = 1_000;
export const XLSX_MAX = 100_000;

const COMMON_KEYS = ["category", "class", "status", "year_from", "year_to", "text"];
export const FILTER_KEYS: Record<FilterScope, string[]> = {
  recalls: [...COMMON_KEYS, "manufacturer", "product_code"],
  entity: [...COMMON_KEYS, "event"],
};

/** Only the filter parameters of an address, in a fixed order (the same filters → the same URL). */
export function filterParams(search: URLSearchParams, scope: FilterScope): URLSearchParams {
  const out = new URLSearchParams();
  for (const key of FILTER_KEYS[scope]) {
    for (const value of search.getAll(key)) {
      if (value.trim()) out.append(key, value);
    }
  }
  return out;
}

export function hasFilters(filters: URLSearchParams): boolean {
  return [...filters.keys()].length > 0;
}

/** One row of the recall finder (the index's recalls table). */
export interface RecallRow extends Recall {
  product_code: string | null;
  manufacturer_name_norm: string | null;
  category: string;
}

export interface RecallPage {
  total: number;
  page: number;
  pages: number;
  page_size: number;
  rows: RecallRow[];
  filters: [string, string][];
  data_as_of: string | null;
}

/** A device page with filters applied (GET /api/export/entity/...?format=json). */
export interface FilteredEntity extends EntityResponse {
  filters: [string, string][];
  /** The device's recall total before the filters. */
  unfiltered_recalls: number;
}

export type ExportTarget =
  | { kind: "recalls" }
  | { kind: "entity"; entityKind: EntityKind; entityId: string }
  | { kind: "search"; q: string };

/** The API path of a file (without the /api base). */
export function exportPath(target: ExportTarget, format: ExportFormat, filters?: URLSearchParams): string {
  const params = new URLSearchParams({ format });
  if (target.kind === "search") params.set("q", target.q);
  for (const [k, v] of filters ?? []) params.append(k, v);
  if (target.kind === "recalls") return `/export/recalls?${params}`;
  if (target.kind === "search") return `/export/search?${params}`;
  return `/export/entity/${encodeURIComponent(target.entityKind)}/${encodeURIComponent(target.entityId)}?${params}`;
}

const SLUG: Record<string, string> = { device: "device", manufacturer: "manufacturer", product_code: "product-code" };

/** The saved file's name, from fixed parts only (never the visitor's text), as the API names it. */
export function fileName(target: ExportTarget, format: ExportFormat, today = new Date()): string {
  const slug = target.kind === "entity" ? (SLUG[target.entityKind] ?? "device") : target.kind;
  const date = today.toISOString().slice(0, 10);
  return `vigil-md-${slug}-${date}.${format}`;
}

/** Saves a file the browser received: a temporary object URL and a link with `download`. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}

/** Fetches a file and saves it. Errors (413 too many rows, 429, 503…) are thrown for the page to show. */
export async function downloadExport(target: ExportTarget, format: ExportFormat, filters?: URLSearchParams): Promise<void> {
  const blob = await api.download(exportPath(target, format, filters));
  saveBlob(blob, fileName(target, format));
}
