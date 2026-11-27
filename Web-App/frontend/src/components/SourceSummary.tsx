import type { EntityKind, PageSourceSummary, SearchSummary } from "../api/types";
import { formatNumber } from "../lib/format";

const KIND_WORD: Record<string, [string, string]> = {
  device: ["device", "devices"],
  manufacturer: ["company", "companies"],
  product_code: ["product code", "product codes"],
};

/**
 * Above the search results: how many records the results hold in each FDA source. Recalls and
 * enforcement reports are counted once (a device and its company often share a recall). MAUDE
 * reports are totalled per kind of result and not added across kinds: a company's or product
 * code's reports include its devices' reports.
 */
export function SourceSummary({ summary }: { summary: SearchSummary }) {
  const n = summary.results;
  const maude = summary.maude
    .map((m) => {
      const [one, many] = KIND_WORD[m.kind] ?? [m.kind, m.kind];
      return `${formatNumber(m.reports)} ${m.reports === 1 ? "report" : "reports"} for ${formatNumber(m.results)} ${m.results === 1 ? one : many}`;
    })
    .join(" · ");
  const rows: { name: string; value: string }[] = [{ name: "MAUDE", value: `${maude} (adverse-event reports)` }];
  if (summary.recalls !== null) {
    rows.push({ name: "Recalls", value: `${formatNumber(summary.recalls)} FDA device ${summary.recalls === 1 ? "recall" : "recalls"}` });
  }
  if (summary.enforcement !== null) {
    rows.push({
      name: "Enforcement",
      value: `${formatNumber(summary.enforcement)} FDA enforcement ${summary.enforcement === 1 ? "report" : "reports"}`,
    });
  }
  return (
    <SourceBox
      title={`What ${n === 1 ? "this result holds" : `these ${formatNumber(n)} results hold`}, by source`}
      rows={rows}
      note={`Recalls and enforcement reports are counted once.${summary.maude.length > 1 ? " MAUDE totals are not added together: a company's reports include its devices' reports." : ""}`}
    />
  );
}

/** The box: a title, one row per FDA source, a short note. */
function SourceBox({ title, rows, note }: { title: string; rows: { name: string; value: string }[]; note?: string }) {
  return (
    <section className="source-summary" aria-label={title}>
      <h2 className="source-summary-title">{title}</h2>
      <dl className="source-summary-list">
        {rows.map((r) => (
          <div key={r.name} className="source-summary-row">
            <dt className="source-name">{r.name}</dt>
            <dd>{r.value}</dd>
          </div>
        ))}
      </dl>
      {note && <p className="hint">{note}</p>}
    </section>
  );
}

const plural = (n: number, one: string, many: string) => `${formatNumber(n)} ${n === 1 ? one : many}`;

/** On a device, company or product code page: what it holds per FDA source (with filters: what matches). */
export function PageSourceSummaryBox({ summary, kind }: { summary: PageSourceSummary; kind: EntityKind }) {
  const what = KIND_WORD[kind]?.[0] ?? "entry";
  const rows = [
    { name: "MAUDE", value: plural(summary.maude_reports, "adverse-event report", "adverse-event reports") },
    { name: "Recalls", value: plural(summary.recalls, "FDA device recall", "FDA device recalls") },
  ];
  if (summary.enforcement !== null) {
    rows.push({ name: "Enforcement", value: plural(summary.enforcement, "FDA enforcement report", "FDA enforcement reports") });
  }
  return (
    <SourceBox
      title={summary.filtered ? `What matches the filters for this ${what}, by source` : `What this ${what} holds, by source`}
      rows={rows}
      note="Enforcement reports are those of the recalls listed: they give a recall its class."
    />
  );
}
