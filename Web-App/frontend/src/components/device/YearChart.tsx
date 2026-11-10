import { useId } from "react";
import { formatNumber } from "../../lib/format";

/**
 * Reports per year as simple horizontal bars (handover §3: "a simple chart is fine").
 * One neutral colour; bars are decorative, the numbers are always written out, and a table
 * with the event-type breakdown is available for screen readers and anyone who wants it.
 */
export function YearChart({ byYear }: { byYear: Record<string, Record<string, number>> }) {
  const tableId = useId();
  const years = Object.keys(byYear ?? {}).sort();
  if (years.length === 0) return null;

  const totals = years.map((y) => Object.values(byYear[y] ?? {}).reduce((a, b) => a + b, 0));
  const max = Math.max(...totals, 1);
  const types = Array.from(new Set(years.flatMap((y) => Object.keys(byYear[y] ?? {}))));

  return (
    <section className="card" aria-labelledby="years-heading">
      <h2 id="years-heading">Reports per year</h2>
      <ul className="bars">
        {years.map((year, i) => {
          const total = totals[i] ?? 0;
          return (
            <li key={year} className="bar-row">
              <span className="bar-label">{year}</span>
              <span className="bar-track" aria-hidden="true">
                <span className="bar-fill" style={{ width: `${(total / max) * 100}%` }} />
              </span>
              <span className="bar-value">{formatNumber(total)}</span>
            </li>
          );
        })}
      </ul>
      <details className="more" id={tableId}>
        <summary>Reports per year by event type (table)</summary>
        <div className="table-scroll">
          <table>
            <caption className="visually-hidden">Reports per year by event type</caption>
            <thead>
              <tr>
                <th scope="col">Year</th>
                {types.map((t) => (
                  <th key={t} scope="col">
                    {t}
                  </th>
                ))}
                <th scope="col">Total</th>
              </tr>
            </thead>
            <tbody>
              {years.map((year, i) => (
                <tr key={year}>
                  <th scope="row">{year}</th>
                  {types.map((t) => (
                    <td key={t}>{formatNumber(byYear[year]?.[t] ?? 0)}</td>
                  ))}
                  <td>{formatNumber(totals[i] ?? 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}
