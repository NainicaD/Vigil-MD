"use client";

import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { failureMessage, isAbortError } from "../../api/errors";
import type { HealthResponse } from "../../api/types";
import { buildInfo, config } from "../../config";
import { formatMs, formatTime } from "../../dev/format";

type Result = { attempt: number; ms: number } & ({ ok: true; data: HealthResponse } | { ok: false; message: string });

/** Is the API up, which data and model versions does it run, and how is this front end built. */
export function ServiceStatus() {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<Result | null>(null);

  useEffect(() => {
    const ctrl = new AbortController();
    const start = performance.now();
    const ms = () => Math.round(performance.now() - start);
    api
      .health({ signal: ctrl.signal })
      .then((data) => setResult({ attempt, ms: ms(), ok: true, data }))
      .catch((error: unknown) => {
        if (!isAbortError(error)) setResult({ attempt, ms: ms(), ok: false, message: failureMessage(error) });
      });
    return () => ctrl.abort();
  }, [attempt]);

  const checking = !result || result.attempt !== attempt;
  const health = !checking && result.ok ? result.data : null;

  return (
    <section className="dev-section" aria-labelledby="dev-status">
      <h2 id="dev-status">Service status</h2>
      <div className="dev-grid">
        <div>
          <h3>API</h3>
          <dl className="dev-facts">
            <dt>Status</dt>
            <dd role="status">
              {checking ? "Checking…" : result.ok ? result.data.status : `Not reachable: ${result.message}`}
            </dd>
            <dt>Response time</dt>
            <dd>{checking ? "—" : formatMs(result.ms)}</dd>
            <dt>Dataset</dt>
            <dd>{health?.dataset ?? "—"}</dd>
            <dt>Search index built</dt>
            <dd>{formatTime(health?.index_built)}</dd>
            <dt>Router rules version</dt>
            <dd>{health?.rules_fingerprint ?? "—"}</dd>
            <dt>Answer templates version</dt>
            <dd>{health?.template_fingerprint ?? "—"}</dd>
          </dl>
        </div>
        <div>
          <h3>This front end</h3>
          <dl className="dev-facts">
            <dt>API address</dt>
            <dd>{config.apiBaseUrl}</dd>
            <dt>Example data</dt>
            <dd>{config.useMocks ? "on (answers from src/mocks/)" : "off (real API)"}</dd>
            <dt>Request timeout</dt>
            <dd>{config.requestTimeoutMs / 1000} s</dd>
            <dt>Version</dt>
            <dd>{buildInfo.version ?? "—"}</dd>
            <dt>Built</dt>
            <dd>{formatTime(buildInfo.builtAt)}</dd>
          </dl>
        </div>
      </div>
      <p className="hint">
        The two versions are fingerprints of the guardrail rules and the answer templates; every answer records the
        same ones, so you can tell which version produced it.
      </p>
      <button type="button" className="btn secondary" onClick={() => setAttempt((n) => n + 1)} disabled={checking}>
        Check again
      </button>
    </section>
  );
}
