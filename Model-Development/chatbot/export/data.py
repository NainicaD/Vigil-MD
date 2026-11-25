"""The rows behind the recall finder, a device page and the search list, and their exports.

  recall finder   the `recalls` table of the local search index (build_index.py): no BigQuery
  device page     device_summary(), the same data as /api/entity, plus the recalls query with
                  a higher limit (the same partition, so the same few MB as opening the page)
  search list     DeviceIndex.search(), the same as /api/search, with a higher limit

The screen and the file use the same function here, so their numbers are the same. No export
reads report narratives.
"""
from __future__ import annotations

from device_search.build_index import OTHER, RECALL_COLUMNS, load_categories
from query_layer import backend as ql_backend
from query_layer import queries, summary

from .filters import Filters

PAGE_SIZE = 50                 # recall finder page
PDF_MAX = 1_000                # recalls in a PDF (spec decision 2)
XLSX_MAX = 100_000             # rows in an Excel file (spec decision 2)
SEARCH_MAX = 500               # search list hits in a file
SHOWN_ON_PAGE = 50             # recalls listed on a filtered device page (all are in the files)


class TooManyRows(Exception):
    """More rows than the format allows; the visitor must narrow the filters."""

    def __init__(self, count: int, limit: int):
        super().__init__(f"{count} rows > {limit}")
        self.count, self.limit = count, limit


# ── recall finder (search index) ──────────────────────────────────────────────

def recall_where(f: Filters) -> tuple[str, list]:
    """The WHERE clause and its bound parameters for the index's recalls table. Values are
    always parameters, never pasted into the SQL."""
    where, args = [], []
    if f.categories:
        where.append(f"category IN ({','.join('?' * len(f.categories))})")
        args += list(f.categories)
    if f.classes:
        where.append(f"recall_class IN ({','.join('?' * len(f.classes))})")
        args += [f"Class {c}" for c in f.classes]
    if f.statuses:
        parts = []
        for s in f.statuses:
            if s == "open":
                parts.append("recall_status LIKE 'Open%'")       # "Open, Classified", ...
            else:
                parts.append("recall_status = ?")
                args.append(s.capitalize())
        where.append(f"({' OR '.join(parts)})")
    if f.year_from:
        where.append("record_date >= ?")
        args.append(f"{f.year_from}-01-01")
    if f.year_to:
        where.append("record_date <= ?")
        args.append(f"{f.year_to}-12-31")
    if f.text:
        like = "%" + f.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(product LIKE ? ESCAPE '\\' OR firm LIKE ? ESCAPE '\\' "
                     "OR reason_text LIKE ? ESCAPE '\\')")
        args += [like, like, like]
    if f.manufacturer:
        where.append("manufacturer_name_norm = ?")
        args.append(f.manufacturer)
    if f.product_code:
        where.append("product_code = ?")
        args.append(f.product_code)
    return (" WHERE " + " AND ".join(where)) if where else "", args


def count_recalls(con, f: Filters) -> int:
    where, args = recall_where(f)
    return con.execute(f"SELECT COUNT(*) FROM recalls{where}", args).fetchone()[0]


def find_recalls(con, f: Filters, limit: int, offset: int = 0) -> list[dict]:
    """Matching recalls, newest first."""
    where, args = recall_where(f)
    cols = [*RECALL_COLUMNS, "category"]
    rows = con.execute(f"SELECT {','.join(cols)} FROM recalls{where} "
                       "ORDER BY record_date DESC, recall_number LIMIT ? OFFSET ?",
                       [*args, limit, offset]).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def recall_page(con, f: Filters, page: int) -> dict:
    """One page of the recall finder: PAGE_SIZE rows, the total and the number of pages."""
    total = count_recalls(con, f)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(1, page), pages)
    return {"total": total, "page": page, "pages": pages, "page_size": PAGE_SIZE,
            "rows": find_recalls(con, f, PAGE_SIZE, (page - 1) * PAGE_SIZE)}


def recalls_for_file(con, f: Filters, limit: int) -> list[dict]:
    """All matching recalls for a file; TooManyRows above the format's limit."""
    total = count_recalls(con, f)
    if total > limit:
        raise TooManyRows(total, limit)
    return find_recalls(con, f, limit)


# ── device page (summary tables) ──────────────────────────────────────────────

def recall_matches(r: dict, f: Filters) -> bool:
    """The recall filters as a Python check, for a device's rows (same rules as recall_where)."""
    if f.categories and r.get("category") not in f.categories:
        return False
    if f.classes and r.get("recall_class") not in {f"Class {c}" for c in f.classes}:
        return False
    if f.statuses:
        status = r.get("recall_status") or ""
        ok = any(status.startswith("Open") if s == "open" else status == s.capitalize()
                 for s in f.statuses)
        if not ok:
            return False
    date = str(r.get("record_date") or "")[:10]
    if f.year_from and (not date or date < f"{f.year_from}-01-01"):
        return False
    if f.year_to and (not date or date > f"{f.year_to}-12-31"):
        return False
    if f.text:
        needle = f.text.lower()
        if not any(needle in (r.get(c) or "").lower() for c in ("product", "firm", "reason_text")):
            return False
    return True


def filter_counts(counts: dict, f: Filters) -> dict:
    """summarize_counts() output, limited to the years and event types asked for."""
    if not (f.year_from or f.year_to or f.events):
        return counts
    keep_event = (lambda e: e in f.events) if f.events else (lambda e: True)
    by_year = {}
    for year, by_event in counts["by_year"].items():
        y = int(year)
        if (f.year_from and y < f.year_from) or (f.year_to and y > f.year_to):
            continue
        kept = {e: n for e, n in by_event.items() if keep_event(e)}
        if kept:
            by_year[year] = kept
    if f.year_from or f.year_to:            # reports without a year drop out of a year range
        by_type: dict[str, int] = {}
        for by_event in by_year.values():
            for e, n in by_event.items():
                by_type[e] = by_type.get(e, 0) + n
    else:
        by_type = {e: n for e, n in counts["by_event_type"].items() if keep_event(e)}
    by_type = dict(sorted(by_type.items(), key=lambda kv: -kv[1]))
    return {"total_reports": sum(by_type.values()), "by_event_type": by_type, "by_year": by_year}


def with_category(rows: list[dict], categories: dict[str, str] | None = None) -> list[dict]:
    """Adds the category (from recall_categories.json) to recall rows from the summary tables."""
    categories = load_categories() if categories is None else categories
    return [{**r, "category": categories.get((r.get("root_cause") or "").strip(), OTHER)}
            for r in rows]


def entity_view(backend, kind: str, entity_id: str, f: Filters, budget: ql_backend.Budget,
                limit: int = XLSX_MAX) -> dict:
    """A device page's data with the filters applied: the page (JSON) and both files use it.

    Returns device_summary()'s fields, with counts and recalls filtered, recalls.shown holding
    the newest SHOWN_ON_PAGE matches, plus `all_recalls` (every match, for the files) and
    `unfiltered_recalls` (the device's recall total). Raises TooManyRows above `limit`."""
    page = summary.device_summary(backend, kind, entity_id, budget)
    total = page["recalls"]["total"]
    if total > XLSX_MAX:                                  # before reading them all
        raise TooManyRows(total, XLSX_MAX)
    rows = page["recalls"]["shown"]
    if total > len(rows):                 # the page holds only the newest MAX_RECALLS
        (rows,), records = backend.run(
            [queries.recalls(backend.table, kind, entity_id, limit=XLSX_MAX)], budget)
        page["sources"] = page["sources"] + summary._sources(records)
    rows = with_category([{k: v for k, v in r.items() if k != "total_recalls"} for r in rows])
    matches = [r for r in rows if recall_matches(r, f)]
    if len(matches) > limit:
        raise TooManyRows(len(matches), limit)
    page["counts"] = filter_counts(page["counts"], f)
    page["recalls"] = {"total": len(matches), "shown": matches[:SHOWN_ON_PAGE]}
    page["unfiltered_recalls"] = total
    page["all_recalls"] = matches
    return page


# ── search list (search index) ────────────────────────────────────────────────

def search_hits(index, q: str, limit: int = SEARCH_MAX) -> list:
    """The same hits, in the same order, as /api/search for the same text."""
    return index.search(q, limit=limit)
