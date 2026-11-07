"""What the chatbot fetches for a device, as plain dicts (JSON-friendly).

  device_summary  always: report counts (by event type and year), top problems, recalls
  narratives      when asked: a few report excerpts, newest first, one per event type in turn
  identity        when asked: who makes it (GUDID)

Every result has "sources": the queries run (SQL, parameters, bytes), so an answer can cite
them and the gate can check its numbers against them. One Budget per user question.
"""
from __future__ import annotations

from collections import defaultdict

from . import config, queries
from .backend import BigQueryBackend, Budget, QueryRecord


def _sources(records: list[QueryRecord]) -> list[dict]:
    return [{"query": r.name, "params": r.params, "tables": list(r.tables),
             "estimated_bytes": r.estimated_bytes, "billed_bytes": r.billed_bytes,
             "rows": r.rows, "sql": r.sql} for r in records]


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


def device_summary(backend: BigQueryBackend, key_type: str, key, budget: Budget | None = None,
                   recall_key: tuple[str, str] | None = None) -> dict:
    """Counts, top problems and recalls for one device key, in one budget-checked step.
    Recalls have no UDI: for udi_di / udi_dis keys pass recall_key (e.g. ("brand",
    "dreamstation"), as device_search's query_key gives); without it recalls are skipped
    with a note."""
    budget = budget or Budget()
    qs = [queries.counts(backend.table, key_type, key),
          queries.problems(backend.table, key_type, key)]
    if key_type in queries.RECALL_FILTER:
        recall_key = (key_type, key)
    with_recalls = recall_key is not None
    if with_recalls:
        qs.append(queries.recalls(backend.table, *recall_key))
    results, records = backend.run(qs, budget)
    recall_rows = results[2] if with_recalls else []
    return {
        "key": {"type": key_type, "value": key if key_type not in config.LIST_KEY_TYPES
                else f"{len(key)} UDIs"},
        "counts": summarize_counts(results[0]),
        "problems": summarize_problems(results[1]),
        "recalls": {
            "matched_by": list(recall_key) if with_recalls else None,
            "total": recall_rows[0]["total_recalls"] if recall_rows else 0,
            "shown": [{k: v for k, v in r.items() if k != "total_recalls"} for r in recall_rows],
            "note": None if with_recalls else
                    "Recalls are not linked by UDI; look the device up by brand or product code.",
        },
        "data_as_of": backend.data_as_of([t for q in qs for t in q.tables]),
        "sources": _sources(records),
        "budget_gb": {"max": budget.max_gb, "spent": round(budget.spent_gb, 4)},
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


def narratives(backend: BigQueryBackend, key_type: str, key: str,
               n: int = config.NARRATIVES_PER_QUESTION,
               max_chars: int = config.NARRATIVE_MAX_CHARS,
               budget: Budget | None = None) -> dict:
    """A few report excerpts. Step 1 picks candidate reports (newest per event type);
    step 2 reads only their texts, in their months of maude_text."""
    budget = budget or Budget()
    (cands,), rec1 = backend.run([queries.narrative_candidates(backend.table, key_type, key)],
                                 budget)
    if not cands:
        return {"key": {"type": key_type, "value": key}, "excerpts": [],
                "sources": _sources(rec1), "budget_gb": {"max": budget.max_gb,
                                                        "spent": round(budget.spent_gb, 4)}}
    keys = [c["mdr_report_key"] for c in cands]
    dates = sorted({c["record_date"] for c in cands if c["record_date"]})
    (texts,), rec2 = backend.run([queries.narrative_texts(backend.table, keys, dates,
                                                          max_chars=max_chars)], budget)
    chosen = pick_narratives(texts, n)
    return {
        "key": {"type": key_type, "value": key},
        "excerpts": [{"mdr_report_key": r["mdr_report_key"], "event_type": r["event_type"],
                      "date_received": r["date_received"],
                      "text": excerpt(r["text"], r["text_length"], max_chars),
                      "shortened": r["text_length"] > max_chars} for r in chosen],
        "sources": _sources(rec1 + rec2),
        "budget_gb": {"max": budget.max_gb, "spent": round(budget.spent_gb, 4)},
    }


def identity(backend: BigQueryBackend, key_type: str, key: str,
             budget: Budget | None = None) -> dict:
    """GUDID records for the key, grouped by manufacturer, device name, model, codes."""
    budget = budget or Budget()
    (rows,), records = backend.run([queries.identity(backend.table, key_type, key)], budget)
    return {"key": {"type": key_type, "value": key}, "devices": rows,
            "sources": _sources(records),
            "budget_gb": {"max": budget.max_gb, "spent": round(budget.spent_gb, 4)}}
