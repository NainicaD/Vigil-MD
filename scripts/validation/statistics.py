"""Per-column statistics for one load of a staging table.

One BigQuery query per table scans the partition once and returns, for every
business column in schemas.FLAT_COLUMNS:
  all columns     non_null, null_pct, distinct_approx
  STRING          blank (empty or whitespace only), length min / p50 / p99 / max,
                  top_values (5 most frequent, values cut to 80 characters;
                  not for merge keys)
  DATE, INT64,    min, max
  FLOAT64, TIMESTAMP
  REPEATED        avg_items, empty_pct (non_null / distinct do not apply)

build_stats_sql and parse_stats_row are pure functions (unit-tested without
BigQuery). Results are written as JSON, one file per source and load.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.warehouse import schemas  # noqa: E402

TOP_N = 5
TOP_VALUE_CHARS = 80
ORDERED_TYPES = {"DATE", "INT64", "INTEGER", "FLOAT64", "FLOAT", "TIMESTAMP"}
SEP = "__"   # alias separator: <column>__<stat>


def _exprs_for(col: str, typ: str, mode: str, key: bool = False) -> list[tuple[str, str]]:
    """(stat name, SQL expression) pairs for one column. Keys get no top_values."""
    q = f"`{col}`"
    if mode.upper() == "REPEATED":
        return [("avg_items", f"AVG(ARRAY_LENGTH({q}))"),
                ("empty_pct", f"ROUND(100 * COUNTIF(ARRAY_LENGTH({q}) = 0) / COUNT(*), 4)")]
    out = [("non_null", f"COUNT({q})"),
           ("null_pct", f"ROUND(100 * COUNTIF({q} IS NULL) / COUNT(*), 4)"),
           ("distinct_approx", f"APPROX_COUNT_DISTINCT({q})")]
    t = typ.upper()
    if t == "STRING":
        out += [("blank", f"COUNTIF(TRIM({q}) = '')"),
                ("len_min", f"MIN(LENGTH({q}))"),
                ("len_p50", f"APPROX_QUANTILES(LENGTH({q}), 100)[SAFE_OFFSET(50)]"),
                ("len_p99", f"APPROX_QUANTILES(LENGTH({q}), 100)[SAFE_OFFSET(99)]"),
                ("len_max", f"MAX(LENGTH({q}))")]
        if not key:   # top values of a unique key are meaningless
            out.append(("top_values",
                        f"APPROX_TOP_COUNT(SUBSTR({q}, 1, {TOP_VALUE_CHARS}), {TOP_N})"))
    elif t in ORDERED_TYPES:
        out += [("min", f"MIN({q})"), ("max", f"MAX({q})")]
    return out


def build_stats_sql(project: str, source: str, ingest_date: dt.date) -> str:
    """One query that computes every statistic for one ingest partition."""
    selects = ["COUNT(*) AS _rows"]
    keys = set(schemas.MERGE_KEYS[source])
    for col, typ, mode in schemas.FLAT_COLUMNS[source]:
        for stat, expr in _exprs_for(col, typ, mode, key=col in keys):
            selects.append(f"{expr} AS {col}{SEP}{stat}")
    return ("SELECT\n  " + ",\n  ".join(selects) +
            f"\nFROM `{schemas.staging_table(project, source)}`"
            f"\nWHERE _ingest_date = DATE '{ingest_date.isoformat()}'")


def _plain(v):
    """JSON-friendly values (dates, decimals, BigQuery row structs)."""
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if hasattr(v, "items"):                      # BigQuery Row / struct
        return {k: _plain(x) for k, x in v.items()}
    if v is not None and not isinstance(v, (int, float, str, bool)):
        return float(v)                          # e.g. Decimal
    return v


def parse_stats_row(source: str, row: dict) -> dict:
    """Turn the flat result row into {"rows": n, "columns": {col: {stat: value}}}."""
    columns: dict[str, dict] = {c: {} for c, _, _ in schemas.FLAT_COLUMNS[source]}
    for key, value in row.items():
        if key == "_rows" or SEP not in key:
            continue
        col, stat = key.split(SEP, 1)
        if col in columns:
            columns[col][stat] = _plain(value)
    return {"rows": _plain(row.get("_rows")), "columns": columns}


def stats_document(project: str, source: str, ingest_date: dt.date, parsed: dict,
                   generated_at: dt.datetime | None = None) -> dict:
    """The JSON written to disk: identification plus the parsed statistics."""
    return {
        "table": schemas.staging_table(project, source),
        "source": source,
        "ingest_date": ingest_date.isoformat(),
        "generated_at": (generated_at or dt.datetime.now(dt.timezone.utc)).isoformat(),
        **parsed,
    }


def write_stats(doc: dict, out_dir: Path) -> Path:
    """reports/statistics/<source>/<ingest_date>.json (overwrites the same load)."""
    path = Path(out_dir) / doc["source"] / f"{doc['ingest_date']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    return path
