"use client";

import { useState, type FormEvent } from "react";
import { api } from "../../api/client";
import { failureMessage } from "../../api/errors";
import {
  ENTITY_KINDS,
  MAX_QUESTION_LENGTH,
  isEntityKind,
  matchToEntity,
  type AnswerDetails,
  type AnswerEntity,
  type EntityKind,
} from "../../api/types";
import type { Turn } from "../../chat/chatStore";
import { config } from "../../config";
import { formatGb, formatMs, formatTime } from "../../dev/format";
import { KIND_LABEL } from "../../lib/format";
import { BotReply } from "../chat/BotReply";
import { Chips } from "./Chips";

type Run =
  | { question: string; entity: AnswerEntity | null; state: "pending" }
  | { question: string; entity: AnswerEntity | null; state: "done"; response: AnswerDetails; ms: number }
  | { question: string; entity: AnswerEntity | null; state: "failed"; message: string; ms: number };

/** Ask one question and see how it was answered: router, device search, data, cost, raw JSON. */
export function AnswerInspector() {
  const [question, setQuestion] = useState("");
  const [kind, setKind] = useState<EntityKind | "">("");
  const [entityId, setEntityId] = useState("");
  const [run, setRun] = useState<Run | null>(null);
  const pending = run?.state === "pending";

  function send(q: string, entity: AnswerEntity | null) {
    if (pending || !q.trim()) return;
    setRun({ question: q, entity, state: "pending" });
    const start = performance.now();
    const ms = () => Math.round(performance.now() - start);
    api
      .answer(q, entity)
      .then((response) => setRun({ question: q, entity, state: "done", response: response as AnswerDetails, ms: ms() }))
      .catch((error: unknown) => setRun({ question: q, entity, state: "failed", message: failureMessage(error), ms: ms() }));
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    const id = entityId.trim();
    send(question.trim(), kind && id ? { entity_type: kind, entity_id: id } : null);
  }

  return (
    <section className="dev-section" aria-labelledby="dev-answer">
      <h2 id="dev-answer">Answer inspector</h2>
      <p className="hint">
        Sends one question to <code>{config.apiBaseUrl}/answer</code>, like the assistant does, and shows every field
        of the reply. Leave the entry empty to let the bot search for the device itself.
      </p>
      <form className="dev-form" onSubmit={submit}>
        <label className="label" htmlFor="dev-question">
          Question
        </label>
        <textarea
          id="dev-question"
          className="dev-input"
          rows={2}
          maxLength={MAX_QUESTION_LENGTH}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <div className="dev-form-row">
          <label className="label" htmlFor="dev-kind">
            About entry (optional)
          </label>
          <select
            id="dev-kind"
            className="dev-input"
            value={kind}
            onChange={(e) => setKind(isEntityKind(e.target.value) ? e.target.value : "")}
          >
            <option value="">No entry: the bot searches</option>
            {ENTITY_KINDS.map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </select>
          <input
            aria-label="Entry id"
            className="dev-input"
            placeholder="id, e.g. dev:000000acme01"
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            disabled={!kind}
          />
          <button type="submit" className="btn" disabled={pending || !question.trim()}>
            {pending ? "Asking…" : "Ask"}
          </button>
        </div>
      </form>

      {run?.state === "failed" && (
        <div className="notice" role="alert">
          <p>
            {run.message} ({formatMs(run.ms)})
          </p>
        </div>
      )}
      {run?.state === "pending" && (
        <p className="hint" role="status">
          Asking…
        </p>
      )}
      {run?.state === "done" && (
        <AnswerDetailsView
          run={run}
          busy={pending}
          onChoose={(match) => {
            setKind(match.type);
            setEntityId(match.id);
            send(run.question, matchToEntity(match));
          }}
          onRetry={() => send(run.question, run.entity)}
        />
      )}
    </section>
  );
}

function AnswerDetailsView({
  run,
  busy,
  onChoose,
  onRetry,
}: {
  run: Extract<Run, { state: "done" }>;
  busy: boolean;
  onChoose: Parameters<typeof BotReply>[0]["onChoose"];
  onRetry: () => void;
}) {
  const r = run.response;
  const turn: Turn = { id: 0, question: run.question, entity: run.entity, about: null, state: "done", response: r };
  const tables = r.sources ? [...new Set(r.sources.flatMap((s) => s.tables))] : null;

  return (
    <div className="dev-result">
      <dl className="dev-facts">
        <dt>Status</dt>
        <dd>{r.status}</dd>
        <dt>Time taken</dt>
        <dd>{formatMs(run.ms)}</dd>
        <dt>BigQuery cost</dt>
        <dd>
          {r.budget_gb ? `${formatGb(r.budget_gb.spent)} of ${formatGb(r.budget_gb.max)} allowed` : "not in this response"}
        </dd>
        <dt>Sections</dt>
        <dd>{r.sections.map((s) => s.kind).join(", ") || "none"}</dd>
      </dl>
      {config.useMocks && (
        <p className="hint">Example answers (mock mode) don't include the router, search and data details.</p>
      )}

      <h3>As the user sees it</h3>
      <div className="dev-preview">
        <BotReply turn={turn} busy={busy} onChoose={onChoose} onRetry={onRetry} />
      </div>

      <div className="dev-grid">
        <div>
          <h3>Router</h3>
          <dl className="dev-facts">
            <dt>Question types</dt>
            <dd>
              <Chips items={r.labels} />
            </dd>
            <dt>Guardrail rules hit</dt>
            <dd>
              <Chips items={r.rule_hits} />
            </dd>
            <dt>Looked up</dt>
            <dd>
              <Chips items={r.run_queries} empty="nothing" />
            </dd>
            <dt>Fixed reply</dt>
            <dd>{r.reply_kind === undefined ? <span className="muted">not in this response</span> : (r.reply_kind ?? "none")}</dd>
          </dl>
        </div>
        <div>
          <h3>Device search</h3>
          {run.entity ? (
            <p>
              Skipped: the question was sent about {run.entity.entity_type} <code>{run.entity.entity_id}</code>.
            </p>
          ) : r.search ? (
            <>
              <dl className="dev-facts">
                <dt>Result</dt>
                <dd>{r.search.status}</dd>
                <dt>Phrase found</dt>
                <dd>{r.search.phrase ? `"${r.search.phrase}"` : "—"}</dd>
              </dl>
              {r.search.matches.length > 0 && (
                <div className="dev-table-wrap">
                  <table className="dev-table">
                    <thead>
                      <tr>
                        <th>Match</th>
                        <th>Kind</th>
                        <th>Id</th>
                        <th>Score</th>
                      </tr>
                    </thead>
                    <tbody>
                      {r.search.matches.map((m) => (
                        <tr key={`${m.type}|${m.id}`}>
                          <td>{m.title}</td>
                          <td>{m.type}</td>
                          <td>
                            <code>{m.id}</code>
                          </td>
                          <td>{m.score ?? "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          ) : (
            <p className="muted">No search in this response.</p>
          )}
        </div>
      </div>

      {r.entities && r.entities.length > 0 && (
        <>
          <h3>Entries looked up</h3>
          <div className="dev-table-wrap">
            <table className="dev-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Kind</th>
                  <th>Id</th>
                  <th>Found</th>
                  <th>Reports</th>
                </tr>
              </thead>
              <tbody>
                {r.entities.map((e) => (
                  <tr key={`${e.type}|${e.id}`}>
                    <td>{e.title ?? "—"}</td>
                    <td>{e.type}</td>
                    <td>
                      <code>{e.id}</code>
                    </td>
                    <td>{e.found ? "yes" : "no"}</td>
                    <td>{e.total_reports ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="dev-grid">
        <div>
          <h3>Queries run</h3>
          {r.sources ? (
            r.sources.length ? (
              <ul className="dev-list">
                {r.sources.map((s, i) => (
                  <li key={i}>
                    <code>{s.query ?? "query"}</code> on {s.tables.join(", ")}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">none</p>
            )
          ) : (
            <p className="muted">not in this response</p>
          )}
        </div>
        <div>
          <h3>Data dates</h3>
          {r.data_as_of && Object.keys(r.data_as_of).length ? (
            <dl className="dev-facts">
              {Object.entries(r.data_as_of).map(([table, date]) => (
                <div key={table} className="dev-facts-row">
                  <dt>{table}</dt>
                  <dd>{formatTime(date)}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="muted">{tables && tables.length ? "no dates given" : "not in this response"}</p>
          )}
        </div>
      </div>

      <dl className="dev-facts">
        <dt>Answer templates version</dt>
        <dd>{r.template_fingerprint ?? "—"}</dd>
        <dt>Router rules version</dt>
        <dd>{r.rules_fingerprint ?? "—"}</dd>
      </dl>

      <details>
        <summary>Raw response (JSON)</summary>
        <pre className="dev-json">{JSON.stringify(r, null, 2)}</pre>
      </details>
    </div>
  );
}
