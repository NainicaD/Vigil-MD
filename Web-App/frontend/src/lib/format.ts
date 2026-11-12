/**
 * Labels and number formats shared by the pages. Counts are reports, never failures or rates
 * (handover §4).
 *
 * Rebuilt on 2026-11-10 (see safeText.tsx); the original author should compare it.
 */
import type { EntityKind } from "../api/types";

export const KIND_LABEL: Record<EntityKind, string> = {
  device: "Device",
  manufacturer: "Company",
  product_code: "Product code",
};

export const KIND_GROUP_LABEL: Record<EntityKind, string> = {
  device: "Devices",
  manufacturer: "Companies",
  product_code: "Product codes",
};

const NUMBER = new Intl.NumberFormat("en-US");

/** "1,234"; a missing number shows as 0. */
export function formatNumber(n: number | null | undefined): string {
  return NUMBER.format(n ?? 0);
}

/** "1 report" or "1,234 reports"; "" when there is no number. */
export function formatReports(n: number | null | undefined): string {
  if (n === null || n === undefined) return "";
  return `${formatNumber(n)} ${n === 1 ? "report" : "reports"}`;
}
