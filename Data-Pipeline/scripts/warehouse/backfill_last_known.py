"""Restore last-known values that a later export blanked in PROD (one-off, issue #18).

The 2026-11-17 MAUDE export blanked the UDI-DI of about 1.4M rows; the merge then copied the
blanks into PROD. Since then the merge keeps the last-known value (schemas.KEEP_LAST_KNOWN), but
the rows blanked before that need this backfill. The values come from an older STAGING
partition (--from-ingest-date, 2026-11-03 for MAUDE: the export before the blanking).

Steps:
  1. add the <column>_removed_at column to PROD if it is missing (ALTER TABLE, free DDL)
  2. count the rows to restore: PROD value NULL now, staging partition has a value
  3. one transaction: UPDATE those rows (value from staging, <column>_removed_at =
     last_updated_at, i.e. the load that blanked it), check the count, COMMIT

Writes need --yes. Without it the script stops after the estimates. --print-only prints the SQL
(no GCP call). The UPDATE is dry-run first and refused above --max-gb.

Usage:
    python scripts/warehouse/backfill_last_known.py --project vigil-md-1792696816 \
        --source maude --from-ingest-date 2026-11-03 --print-only
    python scripts/warehouse/backfill_last_known.py ... --estimate-only   # free dry runs
    python scripts/warehouse/backfill_last_known.py ... --yes             # writes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import schemas  # noqa: E402

log = get_logger("backfill_last_known")
EXIT_OVER_BUDGET, EXIT_NOT_CONFIRMED, EXIT_COUNT_MISMATCH = 2, 3, 4
DEFAULT_MAX_GB = 15.0      # MAUDE PROD 5.8 GB + staging columns (2026-11-21)


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def render_add_column_sql(source: str, prod: str) -> list[str]:
    return [f"ALTER TABLE `{prod}` ADD COLUMN IF NOT EXISTS {removed} TIMESTAMP"
            for removed in schemas.KEEP_LAST_KNOWN[source].values()]


def _match(source: str) -> str:
    return " AND ".join(f"T.{k} = S.{k}" for k in schemas.MERGE_KEYS[source])


def _staging_values(source: str, stg: str, col: str) -> str:
    keys = ", ".join(schemas.MERGE_KEYS[source])
    return (f"SELECT {keys}, {col} FROM `{stg}`\n"
            f"  WHERE _ingest_date = @from_date AND {col} IS NOT NULL")


def render_count_sql(source: str, prod: str, stg: str, col: str) -> str:
    return f"""\
SELECT COUNT(*) AS n
FROM `{prod}` AS T
JOIN ({_staging_values(source, stg, col)}) AS S
  ON {_match(source)}
WHERE T.{col} IS NULL"""


def render_update_sql(source: str, prod: str, stg: str, col: str) -> str:
    removed = schemas.KEEP_LAST_KNOWN[source][col]
    return f"""\
BEGIN TRANSACTION;

UPDATE `{prod}` AS T
SET {col} = S.{col}, {removed} = T.last_updated_at
FROM ({_staging_values(source, stg, col)}) AS S
WHERE {_match(source)}
  AND T.{col} IS NULL;

IF @@row_count != @expected THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('restored %d rows, expected %d', @@row_count, @expected);
END IF;

COMMIT TRANSACTION;"""


# ── BigQuery ──────────────────────────────────────────────────────────────────

def _params(from_date: str, expected: int | None = None) -> list:
    from google.cloud import bigquery
    params = [bigquery.ScalarQueryParameter("from_date", "DATE", from_date)]
    if expected is not None:
        params.append(bigquery.ScalarQueryParameter("expected", "INT64", expected))
    return params


def dry_run_gb(client, sql: str, params: list) -> float:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=params))
    return job.total_bytes_processed / 1e9


def has_column(client, table_id: str, name: str) -> bool:
    return any(f.name == name for f in client.get_table(table_id).schema)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--source", required=True, choices=sorted(schemas.KEEP_LAST_KNOWN))
    p.add_argument("--from-ingest-date", required=True,
                   help="staging partition with the values before the blanking")
    p.add_argument("--max-gb", type=float, default=DEFAULT_MAX_GB)
    p.add_argument("--print-only", action="store_true", help="Print the SQL. No GCP calls.")
    p.add_argument("--estimate-only", action="store_true", help="Dry runs only (free).")
    p.add_argument("--yes", action="store_true", help="Run the writes (ALTER, UPDATE).")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    prod = schemas.prod_table(args.project, args.source)
    stg = schemas.staging_table(args.project, args.source)
    kept = schemas.KEEP_LAST_KNOWN[args.source]

    if args.print_only:
        print("-- 1. add columns\n" + ";\n".join(render_add_column_sql(args.source, prod)) + ";")
        for col in kept:
            print(f"\n-- 2. rows to restore ({col})\n" + render_count_sql(args.source, prod, stg, col))
            print(f"\n-- 3. restore ({col})\n" + render_update_sql(args.source, prod, stg, col))
        return 0

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    write = args.yes and not args.estimate_only

    missing = [r for r in kept.values() if not has_column(client, prod, r)]
    if missing:
        if not write:
            print(f"would add {', '.join(missing)} to {prod} (free DDL); the UPDATE can only be "
                  f"dry-run after that. Run with --yes.")
        else:
            for sql in render_add_column_sql(args.source, prod):
                client.query(sql).result()
            log.info("added %s to %s", ", ".join(missing), prod)

    for col in kept:
        count_sql = render_count_sql(args.source, prod, stg, col)
        gb = dry_run_gb(client, count_sql, _params(args.from_ingest_date))
        print(f"{col}: count dry run {gb:.2f} GB")
        if gb > args.max_gb:
            print(f"STOP: above --max-gb {args.max_gb}")
            return EXIT_OVER_BUDGET
        if missing and not write:
            continue
        n = next(iter(client.query(count_sql, job_config=bigquery.QueryJobConfig(
            query_parameters=_params(args.from_ingest_date),
            maximum_bytes_billed=int(args.max_gb * 1e9))).result())).n
        print(f"{col}: {n} PROD rows are blank now and have a value in staging "
              f"{args.from_ingest_date}")
        update_sql = render_update_sql(args.source, prod, stg, col)
        params = _params(args.from_ingest_date, n)
        gb = dry_run_gb(client, update_sql, params)
        print(f"{col}: UPDATE dry run {gb:.2f} GB (cap {args.max_gb} GB)")
        if gb > args.max_gb:
            print(f"STOP: above --max-gb {args.max_gb}")
            return EXIT_OVER_BUDGET
        if not write:
            print("estimate only: nothing written (run with --yes)")
            return EXIT_NOT_CONFIRMED if not args.estimate_only else 0
        if n:
            client.query(update_sql, job_config=bigquery.QueryJobConfig(
                query_parameters=params, maximum_bytes_billed=int(args.max_gb * 1e9))).result()
        print(f"{col}: restored {n} rows; {kept[col]} = the load that blanked them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
