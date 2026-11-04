"""MERGE one day of STAGING into PROD (upsert).

The SQL is generated from scripts/warehouse/schemas.py (columns, merge keys,
partitioning), so it always matches the tables.

Daily (default):
  1. read the staging partition: row count, min/max _part_date
     (_part_date = COALESCE(record_date, DATE '1900-01-01'))
  2. one BigQuery script:
       BEGIN TRANSACTION
         MERGE prod T USING (staging for @ingest_date) S
           ON key  [AND (T._part_date BETWEEN @min_d AND @max_d
                         OR T._part_date = sentinel)]      ← partition pruning
           WHEN MATCHED AND hash differs → UPDATE
           WHEN NOT MATCHED              → INSERT
         duplicate-key check on prod → ROLLBACK + RAISE if any
       COMMIT TRANSACTION
  No "NOT MATCHED BY SOURCE … DELETE": one day is not a full snapshot.

--rebuild --yes-rebuild (full-load DAG only):
  one transaction: delete every prod row, insert the deduplicated rows of all
  staging partitions, duplicate-key check. The table itself is kept (schema,
  REQUIRED modes, partitioning, clustering).

Usage:
    python scripts/warehouse/merge_prod.py \
        --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25

    # Print the SQL (and the date-stability check) only. No GCP calls:
    python scripts/warehouse/merge_prod.py ... --print-only
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse import schemas
log = get_logger("merge_prod")

# Date pruning on the MERGE. Set a source to False if its record_date can
# change for the same key (check with the date-stability query, --print-only).
PRUNE_BY_DATE = {"enforcement": True, "recall": True, "maude": True, "gudid": False}
SENTINEL = f"DATE '{schemas.PART_DATE_SENTINEL}'"
ALL_STAGING = "_ingest_date >= DATE '1900-01-01'"   # partition filter that keeps every day

assert set(PRUNE_BY_DATE) == set(schemas.SOURCES)
assert all(not PRUNE_BY_DATE[s] or s in schemas.DATE_PARTITIONED_PROD for s in schemas.SOURCES)


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def _biz(source: str) -> list[str]:
    return [n for n, _, _ in schemas.FLAT_COLUMNS[source]]


def _has_part(source: str) -> bool:
    return source in schemas.DATE_PARTITIONED_PROD


def _dup_check(source: str, prod: str) -> str:
    keys = ", ".join(schemas.MERGE_KEYS[source])
    return f"""\
SET dup_keys = (
  SELECT COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys})))
  FROM `{prod}`);
IF dup_keys > 0 THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('{prod}: %d duplicate merge keys after the write. Rolled back.', dup_keys);
END IF;"""


def render_bounds_sql(source: str, stg: str) -> str:
    # CAST: an untyped NULL is INT64 in BigQuery and cannot compare with a DATE
    part = f"COALESCE(record_date, {SENTINEL})" if _has_part(source) else "CAST(NULL AS DATE)"
    return f"""\
SELECT COUNT(*) AS n,
       MIN(IF(p = {SENTINEL}, NULL, p)) AS min_d,
       MAX(IF(p = {SENTINEL}, NULL, p)) AS max_d
FROM (SELECT {part} AS p FROM `{stg}` WHERE _ingest_date = @ingest_date)"""


def render_merge_sql(source: str, prod: str, stg: str) -> str:
    biz = _biz(source)
    keys = schemas.MERGE_KEYS[source]
    has_part = _has_part(source)
    src_cols = biz + ["record_hash"]
    part_sel = f",\n         COALESCE(record_date, {SENTINEL}) AS _part_date" if has_part else ""
    on = " AND ".join(f"T.{k} = S.{k}" for k in keys)
    if PRUNE_BY_DATE[source]:
        on += (f"\n   AND (T._part_date BETWEEN @min_d AND @max_d"
               f" OR T._part_date = {SENTINEL})")
    upd = [f"{c} = S.{c}" for c in src_cols] + (["_part_date = S._part_date"] if has_part else [])
    upd.append("last_updated_at = CURRENT_TIMESTAMP()")
    ins_cols = src_cols + ["first_loaded_at", "last_updated_at"] + (["_part_date"] if has_part else [])
    ins_vals = ([f"S.{c}" for c in src_cols] + ["CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP()"]
                + (["S._part_date"] if has_part else []))
    return f"""\
DECLARE dup_keys INT64;
DECLARE changed INT64;

BEGIN TRANSACTION;

MERGE `{prod}` AS T
USING (
  SELECT {", ".join(src_cols)}{part_sel}
  FROM `{stg}`
  WHERE _ingest_date = @ingest_date
) AS S
ON {on}
WHEN MATCHED AND T.record_hash != S.record_hash THEN
  UPDATE SET {", ".join(upd)}
WHEN NOT MATCHED THEN
  INSERT ({", ".join(ins_cols)})
  VALUES ({", ".join(ins_vals)});

SET changed = @@row_count;

{_dup_check(source, prod)}

COMMIT TRANSACTION;

SELECT changed AS rows_inserted_or_updated, dup_keys;
"""


def render_rebuild_sql(source: str, prod: str, stg: str) -> str:
    biz = _biz(source)
    keys = ", ".join(schemas.MERGE_KEYS[source])
    has_part = _has_part(source)
    src_cols = biz + ["record_hash"]
    ins_cols = src_cols + ["first_loaded_at", "last_updated_at"] + (["_part_date"] if has_part else [])
    part_sel = f", COALESCE(record_date, {SENTINEL})" if has_part else ""
    return f"""\
DECLARE dup_keys INT64;
DECLARE changed INT64;

BEGIN TRANSACTION;

DELETE FROM `{prod}` WHERE TRUE;

INSERT INTO `{prod}` ({", ".join(ins_cols)})
SELECT {", ".join(src_cols)}, first_loaded_at, CURRENT_TIMESTAMP(){part_sel}
FROM (
  SELECT *, MIN(_loaded_at) OVER (PARTITION BY {keys}) AS first_loaded_at
  FROM `{stg}`
  WHERE {ALL_STAGING}
)
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY {keys} ORDER BY _ingest_date DESC, _loaded_at DESC, record_hash) = 1;

SET changed = @@row_count;

{_dup_check(source, prod)}

COMMIT TRANSACTION;

SELECT changed AS rows_inserted_or_updated, dup_keys;
"""


def render_date_stability_sql(source: str, stg: str) -> str:
    """Keys with more than one record_date across all staging history."""
    keys = ", ".join(schemas.MERGE_KEYS[source])
    return f"""\
SELECT COUNT(*) AS keys_with_changed_date
FROM (
  SELECT {keys}
  FROM `{stg}`
  WHERE {ALL_STAGING}
  GROUP BY {keys}
  HAVING COUNT(DISTINCT IFNULL(CAST(record_date AS STRING), 'null')) > 1
)"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project",     required=True)
    parser.add_argument("--source",      required=True, choices=schemas.SOURCES)
    parser.add_argument("--ingest-date", type=lambda s: date.fromisoformat(s).isoformat(),
                        help="YYYY-MM-DD. Required unless --rebuild.")
    parser.add_argument("--rebuild",     action="store_true",
                        help="Replace every prod row from all staging partitions (full load only)")
    parser.add_argument("--yes-rebuild", action="store_true",
                        help="Confirm --rebuild. Without it, --rebuild only prints the target.")
    parser.add_argument("--print-only",  action="store_true",
                        help="Print the SQL, then exit. No GCP calls.")
    args = parser.parse_args()
    if not args.rebuild and not args.ingest_date:
        parser.error("--ingest-date is required unless --rebuild")

    prod = schemas.prod_table(args.project, args.source)
    stg = schemas.staging_table(args.project, args.source)
    schemas.assert_not_protected(prod)

    if args.print_only:
        print(f"-- merge_prod: {args.source}  prune_by_date={PRUNE_BY_DATE[args.source]}")
        if args.rebuild:
            print("-- REBUILD script:\n" + render_rebuild_sql(args.source, prod, stg))
        else:
            print("-- 1. staging bounds:\n" + render_bounds_sql(args.source, stg) + "\n")
            print("-- 2. MERGE script:\n" + render_merge_sql(args.source, prod, stg))
        if "record_date" in _biz(args.source):
            print("-- Date-stability check (run by hand; > 0 means set "
                  f"PRUNE_BY_DATE['{args.source}'] = False):\n"
                  + render_date_stability_sql(args.source, stg))
        return

    if args.rebuild and not args.yes_rebuild:
        print(f"REFUSING: --rebuild deletes every row of {prod} and refills it from "
              f"every partition of {stg}. Run again with --yes-rebuild to confirm.")
        raise SystemExit(2)

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)

    if args.rebuild:
        log.warning("REBUILD %s from all partitions of %s", prod, stg)
        row = next(iter(client.query(render_rebuild_sql(args.source, prod, stg)).result()))
        print(f"\n=== {args.source}: rebuilt {prod}, {row.rows_inserted_or_updated} rows ===")
        return

    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("ingest_date", "DATE", args.ingest_date)])
    b = next(iter(client.query(render_bounds_sql(args.source, stg), job_config=cfg).result()))
    if b.n == 0:
        log.info("%s: staging partition %s is empty — nothing to merge",
                 args.source, args.ingest_date)
        return

    params = [bigquery.ScalarQueryParameter("ingest_date", "DATE", args.ingest_date)]
    if PRUNE_BY_DATE[args.source]:
        # All-NULL dates: an empty range; only the sentinel partition is matched
        params += [bigquery.ScalarQueryParameter("min_d", "DATE", b.min_d or schemas.PART_DATE_SENTINEL),
                   bigquery.ScalarQueryParameter("max_d", "DATE", b.max_d or schemas.PART_DATE_SENTINEL)]
        log.info("%s: %d staged rows, record_date %s … %s", args.source, b.n, b.min_d, b.max_d)
    job = client.query(render_merge_sql(args.source, prod, stg),
                       job_config=bigquery.QueryJobConfig(query_parameters=params))
    row = next(iter(job.result()))
    print(f"\n=== {args.source} {args.ingest_date}: {b.n} staged rows → "
          f"{row.rows_inserted_or_updated} inserted or updated in {prod} "
          f"(0 duplicate keys) ===")


if __name__ == "__main__":
    main()
