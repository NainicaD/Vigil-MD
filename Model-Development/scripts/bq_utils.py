"""Small BigQuery helpers for Model-Development analysis scripts.

Copied (not imported) from Data-Pipeline/scripts/analysis/bias_slices.py and
Data-Pipeline/scripts/validation/run_validation.py so this folder does not
depend on the pipeline's package layout. Keep the read-only rule in sync.

google.cloud.bigquery is imported inside functions so tests and --print-only
need no GCP library.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PROD_DATASET = "vigil_md_prod"
TABLE_PLACEHOLDERS = {"__MAUDE__": "maude", "__GUDID__": "gudid"}
HEADER_KEYS = ("Title", "Check", "Why", "Informs")
PART_MARKER = re.compile(r"^--\s*@part\s+([a-z0-9_]+)\s*$", re.M)

READ_ONLY_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE|ALTER|EXPORT)\b", re.I)


@dataclass
class QueryFile:
    """One .sql file: a header (Title/Check/Why/Informs) and one or more parts."""
    path: Path
    header: dict[str, str]
    parts: list[tuple[str, str]] = field(default_factory=list)   # (part name, raw SQL)

    @property
    def qid(self) -> str:
        return self.path.stem          # e.g. "05_same_model_many_dis"


def prod_table(project: str, source: str) -> str:
    return f"{project}.{PROD_DATASET}.{source}"


def parse_header(text: str) -> dict[str, str]:
    """Read '-- Key: value' lines (continuation lines start with '--   ')."""
    header: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.startswith("--"):
            break
        body = line[2:].strip()
        m = re.match(r"^(%s):\s*(.*)$" % "|".join(HEADER_KEYS), body)
        if m:
            key = m.group(1)
            header[key] = m.group(2).strip()
        elif key and body and not body.startswith("@part"):
            header[key] += " " + body
    return header


def split_parts(text: str) -> list[tuple[str, str]]:
    """Split on '-- @part name' lines. A file with no marker is one part, 'main'."""
    marks = list(PART_MARKER.finditer(text))
    if not marks:
        return [("main", _strip_header(text))]
    parts = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        parts.append((m.group(1), text[m.end():end].strip()))
    return parts


def _strip_header(text: str) -> str:
    lines = text.splitlines()
    while lines and (lines[0].startswith("--") or not lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def load_query_file(path: Path) -> QueryFile:
    text = path.read_text(encoding="utf-8")
    return QueryFile(path=path, header=parse_header(text), parts=split_parts(text))


def load_query_dir(sql_dir: Path, only: list[str] | None = None) -> list[QueryFile]:
    files = sorted(sql_dir.glob("*.sql"))
    if only:
        files = [f for f in files if any(f.stem.startswith(o) for o in only)]
    if not files:
        raise FileNotFoundError(f"no .sql files in {sql_dir} (only={only})")
    return [load_query_file(f) for f in files]


def render(sql: str, project: str) -> str:
    for ph, source in TABLE_PLACEHOLDERS.items():
        sql = sql.replace(ph, prod_table(project, source))
    left = re.findall(r"__[A-Z_]+__", sql)
    if left:
        raise ValueError(f"unknown placeholders left in SQL: {sorted(set(left))}")
    return sql


def _without_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def assert_read_only(sql: str) -> None:
    """Raise unless the SQL is a single SELECT that reads only vigil_md_prod."""
    code = _without_comments(sql).strip()
    head = code.split(None, 1)[0].upper() if code else ""
    if head not in ("SELECT", "WITH", "("):
        raise ValueError(f"not a SELECT: {code[:60]!r}")
    hit = READ_ONLY_FORBIDDEN.search(code)
    if hit:
        raise ValueError(f"write statement in analysis SQL: {hit.group(0)}")
    datasets = set(re.findall(r"`[^`.]+\.([^`.]+)\.[^`]+`", code))
    if datasets != {PROD_DATASET}:
        raise ValueError(f"analysis SQL must read only {PROD_DATASET}, found {sorted(datasets)}")


def estimate_bytes(client, sql: str) -> int:
    """Bytes the query would scan (BigQuery dry run: free, nothing executed)."""
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False))
    return int(job.total_bytes_processed or 0)
