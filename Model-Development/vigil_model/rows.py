"""Turn table cells into the values DeviceSide and MatchContext expect.

Shared by pipeline/s04 (label_set_v1 -> features) and pipeline/s05 (hand sheet -> features)
so the two feature tables cannot drift apart. Deliberately stdlib only: the tests for the
feature rules must run in a venv with no pandas.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

CODE = re.compile(r"[A-Z]{3}")

# alias-table column names, best first; falls back to the first two columns
ALIAS_COLUMNS = [("maude_labeler", "gudid_labeler"),
                 ("maude_manufacturer_norm", "gudid_manufacturer_norm"),
                 ("maude_name", "gudid_name")]


def text(value) -> str | None:
    """A clean string, or None. Treats empty cells, NaN and pandas' null spellings as missing."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:   # NaN is the only value unequal to itself
        return None
    value = str(value).strip()
    # parquet and pandas stringify their own nulls; none is a real identifier
    return None if not value or value in ("nan", "NaN", "<NA>", "None", "NaT") else value


def number(value) -> int | None:
    """An int, or None."""
    value = text(value)
    return None if value is None else int(float(value))


def is_true(value) -> bool:
    value = text(value)
    return value is not None and value.upper() in ("TRUE", "1", "YES")


def product_codes(value) -> tuple[str, ...]:
    """FDA product codes are 3 letters. Accepts a list (GUDID array column) or any separator."""
    if isinstance(value, str) or value is None:
        value = text(value)
        return tuple(CODE.findall(value.upper())) if value else ()
    try:                                  # numpy array, list or other sequence from parquet
        parts = [text(v) for v in value]
    except TypeError:
        return ()
    return tuple(c for p in parts if p for c in CODE.findall(p.upper()))


def load_aliases(path: str | Path) -> frozenset[tuple[str, str]]:
    """Alias pairs (maude labeler, gudid labeler) from the train-only alias table."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return frozenset()
    columns = list(rows[0])
    for m_col, g_col in ALIAS_COLUMNS:
        if m_col in columns and g_col in columns:
            break
    else:
        m_col, g_col = columns[0], columns[1]
    print(f"alias table: {len(rows)} pairs, using columns {m_col!r} -> {g_col!r}")
    pairs = ((text(r[m_col]), text(r[g_col])) for r in rows)
    return frozenset((m, g) for m, g in pairs if m and g)
