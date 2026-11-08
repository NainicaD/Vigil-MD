"""What the chatbot fetches for an entity, as plain dicts (JSON-friendly).

  device_summary  always: the entity (title, company, totals), report counts by event type
                  and year, top problems, recalls
  narratives      when asked: a few report excerpts, newest first, one per event type in turn
  identity        when asked: who makes it, model numbers, product codes

An entity is (entity_type, entity_id): device ("dev:…", from device_search), product_code,
or manufacturer. Every result has "sources": the queries run (SQL, parameters, bytes), so an
answer can cite them and the gate can check its numbers against them. One Budget per user
question. Device report counts are exact UDI links ("reports linked by UDI").
"""
from __future__ import annotations

from collections import defaultdict

from . import config, queries
from .backend import BigQueryBackend, Budget, QueryRecord

LINK_NOTE = {
    "device": "Reports linked to this device by UDI. Reports without a UDI are not counted.",
    "product_code": None,
    "manufacturer": "Reports where this manufacturer name appears, as normalized in MAUDE.",
}


def _sources(records: list[QueryRecord]) -> list[dict]:
    return [{"query": r.name, "params": r.params, "tables": list(r.tables),
             "estimated_bytes": r.estimated_bytes, "billed_bytes": r.billed_bytes,
             "rows": r.rows, "sql": r.sql} for r in records]


def _budget(budget: Budget) -> dict:
    return {"max": budget.max_gb, "spent": round(budget.spent_gb, 4)}


def summarize_counts(rows: list[dict]) -> dict:
    """Rows of (year, event_type, reports) → totals by event type and by year. Pure."""
    by_type: dict[str, int] = defaultdict(int)
    by_year: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by_type[r["event_type"]] += r["reports"]
        if r["year"] is not None:
            by_year[int(r["year"])][r["event_type"]] += r["reports"]
    return {"total_reports": sum(by_type.values()),
            "by_event_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "by_year": {y: dict(v) for y, v in sorted(by_year.items())}}


def summarize_problems(rows: list[dict]) -> dict:
    """Rows of (problem_type, problem_term, reports) → ranked lists + the no-harm count. Pure."""
    out = {"device": [], "patient": [], "no_harm_reports": 0}
    for r in rows:
        if r["problem_type"] == "no_harm":
            out["no_harm_reports"] = r["reports"]
        elif r["problem_type"] in ("device", "patient"):
            out[r["problem_type"]].append({"term": r["problem_term"], "reports": r["reports"]})
    for kind in ("device", "patient"):
        out[kind].sort(key=lambda t: (-t["reports"], t["term"]))
    return out


def device_summary(backend: BigQueryBackend, entity_type: str, entity_id: str,
                   budget: Budget | None = None) -> dict:
    """The entity, counts, top problems and recalls, in one budget-checked step."""
    budget = budget or Budget()
    t = backend.table
    qs = [queries.catalog(t, entity_type, entity_id), queries.counts(t, entity_type, entity_id),
          queries.problems(t, entity_type, entity_id), queries.recalls(t, entity_type, entity_id)]
    (cat, cnt, prob, rec), records = backend.run(qs, budget)
    return {
        "entity": {"type": entity_type, "id": entity_id, "found": bool(cat),
                   **({k: cat[0][k] for k in ("title", "subtitle", "product_codes")} if cat else {})},
        "counts": summarize_counts(cnt),
        "counts_note": LINK_NOTE[entity_type],
        "problems": summarize_problems(prob),
        "recalls": {"total": rec[0]["total_recalls"] if rec else 0,
                    "shown": [{k: v for k, v in r.items() if k != "total_recalls"} for r in rec]},
        "data_as_of": backend.data_as_of([q.tables[0] for q in qs]),
        "sources": _sources(records),
        "budget_gb": _budget(budget),
    }


def excerpt(text: str, text_length: int, max_chars: int = config.NARRATIVE_MAX_CHARS) -> str:
    """At most max_chars, cut at a word boundary, "…" when shortened. Pure."""
    text = (text or "").strip()
    if text_length <= max_chars and len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut[max_chars // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,.;:") + "…"


def pick_narratives(rows: list[dict], n: int = config.NARRATIVES_PER_QUESTION,
                    order=config.NARRATIVE_EVENT_ORDER) -> list[dict]:
    """One text per report; then take in turn one per event type (Death, Injury, Malfunction),
    newest first, until n. Deterministic. Pure."""
    per_report: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (r["mdr_report_key"], str(r["mdr_text_key"]))):
        per_report.setdefault(r["mdr_report_key"], r)
    queues = {et: sorted((r for r in per_report.values() if r["event_type"] == et),
                         key=lambda r: (r["date_received"] or "", r["mdr_report_key"]),
                         reverse=True) for et in order}
    picked = []
    while len(picked) < n and any(queues.values()):
        for et in order:
            if queues[et] and len(picked) < n:
                picked.append(queues[et].pop(0))
    return picked


def narratives(backend: BigQueryBackend, entity_type: str, entity_id: str,
               n: int = config.NARRATIVES_PER_QUESTION,
               max_chars: int = config.NARRATIVE_MAX_CHARS,
               budget: Budget | None = None) -> dict:
    """A few report excerpts. Step 1 picks candidate reports (newest per event type) from
    entity_reports; step 2 reads only their texts, in their months of maude_text."""
    budget = budget or Budget()
    entity = {"type": entity_type, "id": entity_id}
    (cands,), rec1 = backend.run(
        [queries.narrative_candidates(backend.table, entity_type, entity_id)], budget)
    if not cands:
        return {"entity": entity, "excerpts": [], "sources": _sources(rec1),
                "budget_gb": _budget(budget)}
    keys = [c["mdr_report_key"] for c in cands]
    dates = sorted({c["record_date"] for c in cands if c["record_date"]})
    (texts,), rec2 = backend.run([queries.narrative_texts(backend.table, keys, dates,
                                                          max_chars=max_chars)], budget)
    chosen = pick_narratives(texts, n)
    return {
        "entity": entity,
        "excerpts": [{"mdr_report_key": r["mdr_report_key"], "event_type": r["event_type"],
                      "date_received": r["date_received"],
                      "text": excerpt(r["text"], r["text_length"], max_chars),
                      "shortened": r["text_length"] > max_chars} for r in chosen],
        "sources": _sources(rec1 + rec2),
        "budget_gb": _budget(budget),
    }


def identity(backend: BigQueryBackend, entity_type: str, entity_id: str,
             budget: Budget | None = None) -> dict:
    """Who makes it: for a device, its GUDID name, company, model numbers and product codes;
    for a product code or manufacturer, its catalog entry (top brand names, product codes)."""
    budget = budget or Budget()
    if entity_type == "device":
        (rows,), records = backend.run([queries.device_details(backend.table, entity_id)], budget)
    else:
        (rows,), records = backend.run([queries.catalog(backend.table, entity_type, entity_id)],
                                       budget)
    return {"entity": {"type": entity_type, "id": entity_id, "found": bool(rows)},
            "details": rows[0] if rows else None, "sources": _sources(records),
            "budget_gb": _budget(budget)}
