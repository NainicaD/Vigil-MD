"""Build one day of STAGING from RAW: flatten, cast, deduplicate.

Reads only the raw partition _ingest_date = @ingest_date (partition pruning).
Per source, the column logic lives in scripts/warehouse/sql/staging_{source}.sql
(a port of scripts/preprocessing/flatten_to_parquet.py). This script wraps it:

  CREATE TEMP TABLE flat      flattened rows + record_hash (one raw scan)
  null-key count              rows whose merge key is NULL are not staged
  BEGIN TRANSACTION
    DELETE staging rows for _ingest_date
    INSERT deduplicated rows  one row per merge key: newest _loaded_at,
                              ties broken by record_hash (deterministic)
  COMMIT TRANSACTION

Running the same ingest date again replaces that day's staging rows.

Usage:
    python scripts/warehouse/build_staging.py \
        --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25

    # Print the rendered SQL only. No GCP calls:
    python scripts/warehouse/build_staging.py ... --print-only
"""
from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse import schemas
log = get_logger("build_staging")

SQL_DIR   = Path(__file__).resolve().parent / "sql"
RAW_TOKEN = "__RAW_TABLE__"

# Same rules as normalize_date / normalize_manufacturer + SUFFIXES
# (flatten_to_parquet.py:32-61). Anchored formats, like the Python regexes.
FUNCTIONS = r"""
CREATE TEMP FUNCTION norm_date(v STRING) AS ((
  CASE
    WHEN REGEXP_CONTAINS(TRIM(v), r'^\d{8}$')            THEN SAFE.PARSE_DATE('%Y%m%d',   TRIM(v))
    WHEN REGEXP_CONTAINS(TRIM(v), r'^\d{2}/\d{2}/\d{4}$') THEN SAFE.PARSE_DATE('%m/%d/%Y', TRIM(v))
    WHEN REGEXP_CONTAINS(TRIM(v), r'^\d{4}-\d{2}-\d{2}$') THEN SAFE.PARSE_DATE('%Y-%m-%d', TRIM(v))
  END
));

CREATE TEMP FUNCTION norm_mfr(v STRING) AS ((
  NULLIF(REGEXP_REPLACE(
    TRIM(REGEXP_REPLACE(LOWER(TRIM(v)),
      r',?\s*(inc\.?|incorporated|corp\.?|corporation|llc|ltd\.?|limited|co\.?|company|gmbh|s\.a\.?|b\.v\.?)$',
      ''), ' ,.'),
    r'\s+', ' '), '')
));
"""


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def read_source_sql(source: str) -> str:
    path = SQL_DIR / f"staging_{source}.sql"
    if not path.exists():
        raise FileNotFoundError(
            f"{path.name} does not exist yet"
            + (" (GUDID columns and merge key wait for the user, G4)" if source == "gudid" else ""))
    return path.read_text()


def validate_source_sql(source: str, sql: str) -> None:
    """Raise unless the file selects every staging column and filters the partition."""
    if RAW_TOKEN not in sql:
        raise ValueError(f"staging_{source}.sql: no {RAW_TOKEN}")
    if not re.search(r"_ingest_date\s*=\s*@ingest_date", sql):
        raise ValueError(f"staging_{source}.sql: no '_ingest_date = @ingest_date' "
                         "filter (required for partition pruning)")
    body = re.sub(r"--[^\n]*", "", sql)
    wanted = [n for n, _, _ in schemas.FLAT_COLUMNS[source]] + ["_ingest_date", "_loaded_at"]
    missing = [c for c in wanted
               if not re.search(rf"(\bAS\s+{c}\b|^\s*(\w+\.)?{c}\s*,?\s*$)", body, re.M)]
    if missing:
        raise ValueError(f"staging_{source}.sql: columns not selected: {missing}")


def render_staging_sql(source: str, raw: str, stg: str, source_sql: str) -> str:
    """The full BigQuery script for one source and one ingest date. Pure function."""
    validate_source_sql(source, source_sql)
    biz = [n for n, _, _ in schemas.FLAT_COLUMNS[source]]
    keys = schemas.MERGE_KEYS[source]
    stg_cols = biz + [n for n, _, _ in schemas.STAGING_EXTRA]
    select_sql = source_sql.replace(RAW_TOKEN, raw).strip().rstrip(";")
    any_null = " OR ".join(f"{k} IS NULL" for k in keys)
    no_null = " AND ".join(f"{k} IS NOT NULL" for k in keys)
    return f"""\
DECLARE null_key_rows INT64;
{FUNCTIONS}
CREATE TEMP TABLE flat AS
WITH src AS (
{select_sql}
)
SELECT *, TO_HEX(SHA256(TO_JSON_STRING(STRUCT({", ".join(biz)})))) AS record_hash
FROM src;

SET null_key_rows = (SELECT COUNT(*) FROM flat WHERE {any_null});

BEGIN TRANSACTION;

DELETE FROM `{stg}` WHERE _ingest_date = @ingest_date;

INSERT INTO `{stg}` ({", ".join(stg_cols)})
SELECT {", ".join(stg_cols)}
FROM flat
WHERE {no_null}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY {", ".join(keys)} ORDER BY _loaded_at DESC, record_hash) = 1;

COMMIT TRANSACTION;

SELECT
  (SELECT COUNT(*) FROM flat)                                    AS flat_rows,
  null_key_rows,
  (SELECT COUNT(*) FROM `{stg}` WHERE _ingest_date = @ingest_date) AS staged_rows;
"""


def render_raw_count_sql(raw: str) -> str:
    return f"SELECT COUNT(*) AS n FROM `{raw}` WHERE _ingest_date = @ingest_date"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project",     required=True)
    parser.add_argument("--source",      required=True, choices=schemas.SOURCES)
    parser.add_argument("--ingest-date", required=True,
                        type=lambda s: date.fromisoformat(s).isoformat())
    parser.add_argument("--print-only",  action="store_true",
                        help="Print the rendered SQL, then exit. No GCP calls.")
    args = parser.parse_args()

    raw = schemas.raw_table(args.project, args.source)
    stg = schemas.staging_table(args.project, args.source)
    sql = render_staging_sql(args.source, raw, stg, read_source_sql(args.source))

    if args.print_only:
        print(f"-- build_staging: {args.source}, @ingest_date = {args.ingest_date}")
        print(f"-- 1. {render_raw_count_sql(raw)}   (0 rows → exit 0)")
        print("-- 2. script:")
        print(sql)
        return

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("ingest_date", "DATE", args.ingest_date)])

    n = next(iter(client.query(render_raw_count_sql(raw), job_config=cfg).result())).n
    if n == 0:
        log.info("%s: raw partition %s has no rows — nothing to stage",
                 args.source, args.ingest_date)
        return

    row = next(iter(client.query(sql, job_config=cfg).result()))
    if row.null_key_rows:
        log.warning("%s: %d rows have a NULL merge key %s and were not staged",
                    args.source, row.null_key_rows, schemas.MERGE_KEYS[args.source])
    print(f"\n=== {args.source} {args.ingest_date}: {n} raw rows → {row.flat_rows} flat rows "
          f"→ {row.staged_rows} staged ({row.null_key_rows} null-key rows dropped, "
          f"{row.flat_rows - row.null_key_rows - row.staged_rows} duplicates removed) ===")


if __name__ == "__main__":
    main()
