import { useEffect, useId, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import {
  CATEGORY_OPTIONS,
  CLASS_OPTIONS,
  EVENT_OPTIONS,
  FILTER_KEYS,
  MIN_YEAR,
  STATUS_OPTIONS,
  TEXT_MAX,
  TEXT_MIN,
  type FilterScope,
} from "../../api/export";
import type { SearchHit } from "../../api/types";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import "./export.css";

/** Text and product code apply this long after typing stops (the other controls at once). */
export const FILTER_DEBOUNCE_MS = 400;

const YEARS = Array.from({ length: new Date().getFullYear() - MIN_YEAR + 1 }, (_, i) => new Date().getFullYear() - i);

type Option = { readonly value: string; readonly label: string };

/**
 * The filters of the recall finder ("recalls") or a device page ("entity"), kept in the address
 * bar so a shared link shows the same results. Every change goes to page 1.
 */
export function FilterBar({ scope }: { scope: FilterScope }) {
  const [params, setParams] = useSearchParams();
  const id = useId();

  const update = (change: (p: URLSearchParams) => void) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        change(next);
        next.delete("page");
        return next;
      },
      { replace: true },
    );
  const toggle = (key: string, value: string) =>
    update((p) => {
      const values = p.getAll(key);
      p.delete(key);
      const kept = values.includes(value) ? values.filter((v) => v !== value) : [...values, value];
      for (const v of kept) p.append(key, v);
    });
  const setOne = (key: string, value: string | null) =>
    update((p) => (value ? p.set(key, value) : p.delete(key)));

  // Text: applied after typing stops, and only once it is long enough (the API refuses 1 letter).
  // Only a change in what was typed is applied, so "Clear filters" is not undone by the delay.
  const [text, setText] = useState(() => params.get("text") ?? "");
  const typed = useDebouncedValue(text, FILTER_DEBOUNCE_MS).trim();
  const lastTyped = useRef(typed);
  useEffect(() => {
    if (typed === lastTyped.current) return;
    lastTyped.current = typed;
    if (typed.length > 0 && typed.length < TEXT_MIN) return;
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (typed) next.set("text", typed);
        else next.delete("text");
        next.delete("page");
        return next;
      },
      { replace: true },
    );
  }, [typed, setParams]);

  // Product code: applied when it is 3 letters (or emptied).
  const [code, setCode] = useState(() => params.get("product_code") ?? "");
  const onCode = (value: string) => {
    const v = value.replace(/[^A-Za-z]/g, "").slice(0, 3).toUpperCase();
    setCode(v);
    if (v.length === 3 || v.length === 0) setOne("product_code", v || null);
  };

  const clear = () => {
    setText("");
    lastTyped.current = typed;          // the delayed old text must not be applied again
    setCode("");
    update((p) => FILTER_KEYS[scope].forEach((k) => p.delete(k)));
  };

  const active = FILTER_KEYS[scope].some((k) => params.getAll(k).length > 0);

  return (
    <form className="filter-bar" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
      <div className="filter-row">
        {scope === "entity" && (
          <Checkboxes legend="Event type (report counts)" name="event" options={EVENT_OPTIONS} params={params} onToggle={toggle} />
        )}
        <Checkboxes legend="Recall category" name="category" options={CATEGORY_OPTIONS} params={params} onToggle={toggle} />
        <Checkboxes legend="Recall class" name="class" options={CLASS_OPTIONS} params={params} onToggle={toggle} />
        <Checkboxes legend="Recall status" name="status" options={STATUS_OPTIONS} params={params} onToggle={toggle} />
      </div>
      <div className="filter-row">
        <YearSelect label="From year" value={params.get("year_from")} onChange={(v) => setOne("year_from", v)} />
        <YearSelect label="To year" value={params.get("year_to")} onChange={(v) => setOne("year_to", v)} />
        <label className="filter-field">
          <span>Text in product, firm or reason</span>
          <input
            id={`${id}-text`}
            className="filter-input wide"
            type="search"
            value={text}
            maxLength={TEXT_MAX}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. battery"
            autoComplete="off"
          />
        </label>
        {scope === "recalls" && (
          <div className="filter-field">
            <label className="filter-field">
              <span>Product code</span>
              <input
                className="filter-input code"
                value={code}
                onChange={(e) => onCode(e.target.value)}
                placeholder="e.g. FRN"
                aria-describedby={`${id}-code-hint`}
                autoComplete="off"
              />
            </label>
            <span id={`${id}-code-hint`} className="visually-hidden">
              3 letters, applied when complete
            </span>
          </div>
        )}
      </div>
      {scope === "recalls" && (
        <ManufacturerPicker value={params.get("manufacturer")} onChange={(v) => setOne("manufacturer", v)} />
      )}
      <div className="filter-row">
        <button type="button" className="btn secondary small" onClick={clear} disabled={!active && !text && !code}>
          Clear filters
        </button>
      </div>
    </form>
  );
}

function Checkboxes({
  legend,
  name,
  options,
  params,
  onToggle,
}: {
  legend: string;
  name: string;
  options: readonly Option[];
  params: URLSearchParams;
  onToggle: (key: string, value: string) => void;
}) {
  const chosen = params.getAll(name);
  return (
    <fieldset className="filter-group">
      <legend>{legend}</legend>
      <div className="filter-options">
        {options.map((o) => (
          <label key={o.value}>
            <input type="checkbox" checked={chosen.includes(o.value)} onChange={() => onToggle(name, o.value)} />
            {o.label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function YearSelect({ label, value, onChange }: { label: string; value: string | null; onChange: (v: string | null) => void }) {
  return (
    <label className="filter-field">
      <span>{label}</span>
      <select className="filter-select" value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">Any</option>
        {YEARS.map((y) => (
          <option key={y} value={String(y)}>
            {y}
          </option>
        ))}
      </select>
    </label>
  );
}

/** The manufacturer filter: picked from the search box's manufacturer hits. */
function ManufacturerPicker({ value, onChange }: { value: string | null; onChange: (v: string | null) => void }) {
  const [text, setText] = useState("");
  const query = useDebouncedValue(text, FILTER_DEBOUNCE_MS).trim();
  const [found, setFound] = useState<{ query: string; hits: SearchHit[] } | null>(null);

  useEffect(() => {
    if (query.length < 2) return;
    const ctrl = new AbortController();
    api
      .search(query, { signal: ctrl.signal, limit: 25 })
      .then((r) => setFound({ query, hits: r.hits.filter((h) => h.kind === "manufacturer").slice(0, 8) }))
      .catch(() => setFound({ query, hits: [] }));
    return () => ctrl.abort();
  }, [query]);

  if (value) {
    return (
      <div className="filter-row">
        <span className="filter-chip">
          Manufacturer: {value}
          <button type="button" className="btn secondary small" onClick={() => onChange(null)} aria-label={`Remove manufacturer ${value}`}>
            ×
          </button>
        </span>
      </div>
    );
  }
  const hits = found && found.query === query && query.length >= 2 ? found.hits : [];
  return (
    <div className="filter-field">
      <label className="filter-field">
        <span>Manufacturer</span>
        <input
          className="filter-input wide"
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Type a company name, then choose it"
          autoComplete="off"
        />
      </label>
      {hits.length > 0 && (
        <ul className="picker-list" aria-label="Matching manufacturers">
          {hits.map((h) => (
            <li key={h.id}>
              <button type="button" className="btn secondary small" onClick={() => onChange(h.entity_id)}>
                {h.title}
              </button>
            </li>
          ))}
        </ul>
      )}
      {found && found.query === query && query.length >= 2 && hits.length === 0 && (
        <p className="hint">No manufacturer matches “{query}”.</p>
      )}
    </div>
  );
}
