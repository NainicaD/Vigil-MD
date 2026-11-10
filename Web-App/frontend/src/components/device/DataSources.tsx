import type { Source } from "../../api/types";
import { dataDate } from "../../lib/dataDate";

/** Readable names for the summary tables the API reads (same names the chatbot cites). */
const TABLE_NAMES: Record<string, string> = {
  entity_catalog: "FDA MAUDE and GUDID",
  entity_counts: "FDA MAUDE reports",
  entity_problems: "MAUDE problem codes",
  entity_recalls: "FDA recalls and enforcement",
  entity_reports: "FDA MAUDE reports",
  maude_text: "MAUDE report text",
  device_catalog: "FDA GUDID",
};

export function DataSources({
  sources,
  dataAsOf,
}: {
  sources: Source[] | null | undefined;
  dataAsOf: Record<string, string | null> | null | undefined;
}) {
  const tables = Array.from(new Set((sources ?? []).flatMap((s) => s.tables ?? [])));
  const names = Array.from(new Set(tables.map((t) => TABLE_NAMES[t] ?? t)));
  const date = dataDate(dataAsOf);
  if (names.length === 0 && !date) return null;

  return (
    <section className="sources" aria-label="Data sources">
      {names.length > 0 && (
        <p>
          <span className="label-inline">Sources: </span>
          {names.join(", ")}.
        </p>
      )}
      {date && (
        <p>
          <span className="label-inline">Data as of: </span>
          <time dateTime={date}>{date}</time>
        </p>
      )}
    </section>
  );
}
