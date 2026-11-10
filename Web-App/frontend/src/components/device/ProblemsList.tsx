import type { ProblemTerm, Problems } from "../../api/types";
import { formatReports } from "../../lib/format";

/** Top device and patient problems, in the API's order, and the "no patient harm" count. */
export function ProblemsList({ problems }: { problems: Problems }) {
  const device = problems.device ?? [];
  const patient = problems.patient ?? [];
  const noHarm = problems.no_harm_reports;
  if (device.length === 0 && patient.length === 0 && !noHarm) return null;

  return (
    <section className="card" aria-labelledby="problems-heading">
      <h2 id="problems-heading">Reported problems</h2>
      <div className="two-col">
        <TermList title="Top device problems" terms={device} />
        <TermList title="Top patient problems" terms={patient} />
      </div>
      {noHarm !== null && noHarm !== undefined && (
        <p className="no-harm">
          No patient harm reported: <strong>{formatReports(noHarm)}</strong>
        </p>
      )}
    </section>
  );
}

function TermList({ title, terms }: { title: string; terms: ProblemTerm[] }) {
  return (
    <div>
      <h3>{title}</h3>
      {terms.length === 0 ? (
        <p className="muted">None given.</p>
      ) : (
        <ol className="terms">
          {terms.map((t, i) => (
            <li key={`${t.term}-${i}`}>
              <span className="term">{t.term}</span>
              <span className="term-count">{formatReports(t.reports)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
