import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { isEntityKind, type EntityKind, type EntityResponse } from "../api/types";
import { DataSources } from "../components/device/DataSources";
import { ProblemsList } from "../components/device/ProblemsList";
import { RecallsList } from "../components/device/RecallsList";
import { ReportCounts } from "../components/device/ReportCounts";
import { YearChart } from "../components/device/YearChart";
import { useChat } from "../chat/chatStore";
import { useEntity } from "../hooks/useEntity";
import { KIND_LABEL } from "../lib/format";
import { paths } from "../routes";

export const NO_DATA_MESSAGE = "This entry is in the search list, but there is no report or recall data for it.";

export function DevicePage() {
  const { kind, entityId } = useParams();
  if (!isEntityKind(kind) || !entityId) return <PageNotFound />;
  // key: a new entity starts with a fresh page state.
  return <EntityView key={`${kind}|${entityId}`} kind={kind} entityId={entityId} />;
}

function EntityView({ kind, entityId }: { kind: EntityKind; entityId: string }) {
  const { state, retry } = useEntity(kind, entityId);
  const { setPageEntity } = useChat();

  const title = state.status === "done" ? state.data.entity.title || entityId : null;

  // On a device page the assistant sends this entity, so the bot skips its own search
  // (handover §3). Cleared when leaving the page.
  useEffect(() => {
    setPageEntity({ entity: { entity_type: kind, entity_id: entityId }, title: title ?? entityId });
  }, [kind, entityId, title, setPageEntity]);
  useEffect(() => () => setPageEntity(null), [setPageEntity]);
  useEffect(() => {
    document.title = title ? `${title} · Vigil MD` : "Vigil MD";
    return () => {
      document.title = "Vigil MD";
    };
  }, [title]);

  return (
    <div className="page device-page">
      <p className="back">
        <Link to={paths.search()}>← Search</Link>
      </p>
      {state.status === "loading" && (
        <p className="hint" role="status">
          Loading…
        </p>
      )}
      {state.status === "not_found" && <PageNotFound inline />}
      {state.status === "error" && (
        <div className="notice" role="alert">
          <p>{state.message}</p>
          <button type="button" className="btn" onClick={retry}>
            Try again
          </button>
        </div>
      )}
      {state.status === "done" && <EntityContent data={state.data} entityId={entityId} />}
    </div>
  );
}

function EntityContent({ data, entityId }: { data: EntityResponse; entityId: string }) {
  const e = data.entity;
  const codes = e.product_codes ?? [];
  const kindLabel = KIND_LABEL[e.type] ?? e.type;

  return (
    <>
      <header className="entity-head">
        <span className="tag">{kindLabel}</span>
        <h1>{e.title || entityId}</h1>
        {e.subtitle && <p className="entity-company">{e.subtitle}</p>}
        {codes.length > 0 && (
          <p className="entity-codes">
            <span className="label-inline">{codes.length === 1 ? "Product code: " : "Product codes: "}</span>
            {codes.join(", ")}
          </p>
        )}
        <AskButton />
      </header>

      {!e.found ? (
        <p className="notice">{NO_DATA_MESSAGE}</p>
      ) : (
        <div className="cards">
          <ReportCounts counts={data.counts} note={data.counts_note} />
          <YearChart byYear={data.counts?.by_year ?? {}} />
          <ProblemsList problems={data.problems ?? { device: [], patient: [], no_harm_reports: null }} />
          <RecallsList recalls={data.recalls ?? { total: 0, shown: [] }} />
        </div>
      )}

      <DataSources sources={data.sources} dataAsOf={data.data_as_of} />
    </>
  );
}

function AskButton() {
  const { open, setOpen } = useChat();
  if (open) return null;
  return (
    <button type="button" className="btn secondary ask-btn" onClick={() => setOpen(true)}>
      Ask the assistant about this
    </button>
  );
}

function PageNotFound({ inline = false }: { inline?: boolean }) {
  return (
    <div className={inline ? undefined : "page"}>
      <h1>Page not found</h1>
      <p>
        This entry doesn't exist. <Link to={paths.search()}>Search again</Link>.
      </p>
    </div>
  );
}
