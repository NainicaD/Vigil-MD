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
MODEL_DATASET = "vigil_md_model"     # the only dataset any script may write to
TABLE_PLACEHOLDERS = {"__MAUDE__": "maude", "__GUDID__": "gudid"}
HEADER_KEYS = ("Title", "Check", "Why", "Informs")
PART_MARKER = re.compile(r"^--\s*@part\s+([a-z0-9_]+)\s*$", re.M)

# {{ name(args) }}. Arguments may contain parentheses; a top-level comma separates them.
MACRO = re.compile(r"\{\{\s*(\w+)\((.*?)\)\s*\}\}")
# Statements a model-dataset write may not contain. CREATE is allowed once (checked separately).
WRITE_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|TRUNCATE|ALTER|EXPORT)\b", re.I)

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


def split_macro_args(text: str) -> list[str]:
    """Split on commas that are not inside parentheses. '' gives [] (a macro with no arguments)."""
    args, depth, cur = [], 0, ""
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def expand_macros(sql: str, macros: dict) -> str:
    """Replace every {{ name(args) }} with macros[name](*args). An unknown name or a wrong number of
    arguments raises ValueError, so a typo can never reach BigQuery as literal text."""
    def one(m: re.Match) -> str:
        name = m.group(1)
        if name not in macros:
            raise ValueError(f"unknown macro {name!r}")
        try:
            return macros[name](*split_macro_args(m.group(2)))
        except TypeError as e:
            raise ValueError(f"macro {name}: {e}") from e
    out = MACRO.sub(one, sql)
    if "{{" in out or "}}" in out:
        raise ValueError("malformed macro left in SQL (unbalanced {{ }})")
    return out


def render(sql: str, project: str, macros: dict | None = None) -> str:
    """Fill the table placeholders. With `macros` (same_device_features.SQL_MACROS), expand
    {{ name(args) }} first. macros=None does no macro pass, so the profiling queries are unchanged."""
    if macros is not None:
        sql = expand_macros(sql, macros)
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


def assert_model_write_only(sql: str) -> None:
    """Raise unless the SQL is exactly one CREATE OR REPLACE TABLE into vigil_md_model that reads only
    vigil_md_prod and vigil_md_model. A separate guard on purpose: assert_read_only stays strict."""
    code = _without_comments(sql).strip()
    m = re.match(r"CREATE OR REPLACE TABLE `([^`]+)`", code, re.I)
    if not m or len(re.findall(r"\bCREATE\b", code, re.I)) != 1:
        raise ValueError(f"must be exactly one CREATE OR REPLACE TABLE: {code[:60]!r}")
    target = m.group(1).split(".")
    if len(target) != 3 or target[1] != MODEL_DATASET:
        raise ValueError(f"writes outside {MODEL_DATASET}: {m.group(1)}")
    hit = WRITE_FORBIDDEN.search(code)
    if hit:
        raise ValueError(f"forbidden statement: {hit.group(0)}")
    datasets = set(re.findall(r"`[^`.]+\.([^`.]+)\.[^`]+`", code))
    if not datasets <= {PROD_DATASET, MODEL_DATASET}:
        raise ValueError(f"reads outside {PROD_DATASET}/{MODEL_DATASET}: {sorted(datasets)}")

def assert_select_prod_model(sql: str) -> None:
    """Raise unless the SQL is a single SELECT that reads only vigil_md_prod and vigil_md_model.
    For queries that need the model tables (device_split, alias_table) but write nothing. A separate
    guard: assert_read_only stays prod-only for the profiling queries."""
    code = _without_comments(sql).strip()
    head = code.split(None, 1)[0].upper() if code else ""
    if head not in ("SELECT", "WITH", "("):
        raise ValueError(f"not a SELECT: {code[:60]!r}")
    hit = READ_ONLY_FORBIDDEN.search(code)
    if hit:
        raise ValueError(f"write statement in a read query: {hit.group(0)}")
    datasets = set(re.findall(r"`[^`.]+\.([^`.]+)\.[^`]+`", code))
    if not datasets or not datasets <= {PROD_DATASET, MODEL_DATASET}:
        raise ValueError(f"must read only {PROD_DATASET}/{MODEL_DATASET}, found {sorted(datasets)}")


def estimate_bytes(client, sql: str) -> int:
    """Bytes the query would scan (BigQuery dry run: free, nothing executed)."""
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False))
    return int(job.total_bytes_processed or 0)
