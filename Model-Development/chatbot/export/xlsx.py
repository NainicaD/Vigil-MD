"""Builds the Excel file (XlsxWriter, constant memory: each row is written once, in order).

Why XlsxWriter, not openpyxl: on a laptop (2026-11-25), 59,705 recalls took 14.8-16 s with
openpyxl's write-only mode and 100,000 rows 23-26 s (over the 15 s target; Cloud Run stops a
request at 60 s); XlsxWriter took 5.5 s and 9.3 s for the same rows.

Memory: the sheets go through temporary files (in_memory off), not a workbook held in memory:
100,000 rows with unique reasons peaked at about 7 MB of Python memory instead of about 450 MB,
for 13.3 s instead of 10.7 s, so several large downloads at once stay within the 2 GiB service.

Safety: no cell is ever a formula. The workbook is opened with strings_to_formulas off and every
text value goes through write_string, so FDA text that starts with =, +, - or @ stays text.
Control characters Excel does not allow are dropped, and text over Excel's 32,767-character cell
limit is cut there with "…" (FDA reasons are far shorter). Dates are real dates, so sorting works.
"""
from __future__ import annotations

import datetime as dt
import io
import re

import xlsxwriter

from .document import Document, Table

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CELL_MAX = 32_767
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
OPTIONS = {"constant_memory": True, "in_memory": False, "strings_to_formulas": False,
           "strings_to_numbers": False, "strings_to_urls": False}


def clean(value) -> str:
    text = _ILLEGAL.sub("", str(value))
    return text if len(text) <= CELL_MAX else text[:CELL_MAX - 1] + "…"


def _date(value) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    try:
        d = dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return dt.datetime(d.year, d.month, d.day)


def _write(ws, r: int, c: int, value, kind: str, fmt) -> None:
    if value is None or value == "":
        return
    if kind == "int":
        ws.write_number(r, c, int(value))
        return
    if kind == "date":
        d = _date(value)
        if d is not None:
            ws.write_datetime(r, c, d, fmt["date"])
            return
    ws.write_string(r, c, clean(value))


def _about(wb, doc: Document, fmt) -> None:
    ws = wb.add_worksheet("About")
    ws.set_column(0, 0, 30)
    ws.set_column(1, 1, 100)
    lines: list[tuple] = [(doc.title, None, True), ("", None, False)]
    lines += [(k, v, True) for k, v in doc.header] + [("", None, False)]
    lines += [("Filters", None, True)] + [(k, v, False) for k, v in doc.filters]
    if doc.facts and not any(t.name == "Summary" for t in doc.tables):
        lines += [("", None, False), ("Summary by source", None, True)]
        lines += [(k.strip(), v, False) for k, v in doc.facts]
    lines += [("", None, False), ("Notes", None, True)] + [(n, None, False) for n in doc.notes]
    for r, (a, b, bold) in enumerate(lines):
        if a:
            ws.write_string(r, 0, clean(a), fmt["bold"] if bold else None)
        if b is not None:
            ws.write_string(r, 1, clean(b))


def _table(wb, table: Table, fmt) -> None:
    ws = wb.add_worksheet(table.name[:31])
    for c, col in enumerate(table.columns):
        ws.set_column(c, c, col.width)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, len(table.rows), len(table.columns) - 1)
    for c, col in enumerate(table.columns):
        ws.write_string(0, c, col.title, fmt["bold"])
    for r, row in enumerate(table.rows, start=1):
        for c, col in enumerate(table.columns):
            _write(ws, r, c, row.get(col.key), col.kind, fmt)


def build(doc: Document) -> bytes:
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, OPTIONS)
    fmt = {"bold": wb.add_format({"bold": True}),
           "date": wb.add_format({"num_format": "yyyy-mm-dd"})}
    _about(wb, doc, fmt)
    for table in doc.tables:
        _table(wb, table, fmt)
    wb.close()
    return out.getvalue()
