import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { SearchResults } from "../components/SearchResults";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { MIN_QUERY_LENGTH, useSearch } from "../hooks/useSearch";

/** Search 300 ms after typing stops (handover §3). */
export const SEARCH_DEBOUNCE_MS = 300;

export function SearchPage() {
  const [params, setParams] = useSearchParams();
  const [text, setText] = useState(() => params.get("q") ?? "");
  const query = useDebouncedValue(text, SEARCH_DEBOUNCE_MS);
  const state = useSearch(query);

  // Keep the search in the address, so "back" from a device page shows the same results.
  useEffect(() => {
    const q = query.trim();
    if ((params.get("q") ?? "") !== q) setParams(q ? { q } : {}, { replace: true });
  }, [query, params, setParams]);

  const tooShort = text.trim().length > 0 && text.trim().length < MIN_QUERY_LENGTH;

  return (
    <div className="page">
      <h1>Search FDA device reports and recalls</h1>
      <p className="lead">Find a device, a company or a 3-letter FDA product code, then open its page.</p>

      <form className="search-form" role="search" onSubmit={(e) => e.preventDefault()}>
        <label htmlFor="search-input" className="label">
          Device, company or product code
        </label>
        <input
          id="search-input"
          className="search-input"
          type="search"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="e.g. a brand name, a company or a product code"
          autoComplete="off"
          spellCheck={false}
          maxLength={200}
          autoFocus
        />
      </form>

      <div className="search-state" aria-live="polite">
        {tooShort && <p className="hint">Type at least {MIN_QUERY_LENGTH} characters.</p>}
        {state.status === "loading" && <p className="hint">Searching…</p>}
        {state.status === "error" && <p className="notice">{state.message}</p>}
      </div>
      {state.status === "done" && <SearchResults hits={state.hits} />}
    </div>
  );
}
