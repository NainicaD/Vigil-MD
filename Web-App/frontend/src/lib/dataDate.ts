/**
 * The date the data is "as of": the oldest last-modified date of the tables read, as the
 * chatbot's own footer gives it (answer/render.py footer()). YYYY-MM-DD, or null.
 *
 * Rebuilt on 2026-11-10 (see safeText.tsx); the original author should compare it.
 */
export function dataDate(dataAsOf: Record<string, string | null> | null | undefined): string | null {
  const days = Object.values(dataAsOf ?? {})
    .filter((v): v is string => typeof v === "string" && /^\d{4}-\d{2}-\d{2}/.test(v))
    .map((v) => v.slice(0, 10))
    .sort();
  return days[0] ?? null;
}
