"""Validate the staging tables: schema, then Great Expectations value checks.

1. Schema: each staging table's live schema (BigQuery metadata, no data read)
   is compared with schemas.py.
2. Values (--values): the Great Expectations suite from expectations.py runs on
   one load (_ingest_date partition), reading only the columns it needs. A dry
   run estimates the bytes first and stops above --max-gb.

Usage:
    python scripts/validation/run_validation.py --project vigil-md-1792696816
    python scripts/validation/run_validation.py --project ... --source enforcement --values
    python scripts/validation/run_validation.py --project ... --source recall --values \
        --ingest-date 2026-10-26 --estimate-only

Exit codes: 0 = all passed, 3 = a check failed (a data failure, not worth
retrying), anything else = unexpected error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger                       # noqa: E402
from scripts.validation.expectations import (               # noqa: E402
    build_suite, columns_for_query, compare_schema, expected_schema)
from scripts.warehouse import schemas                       # noqa: E402

log = get_logger("run_validation")
EXIT_OK, EXIT_DATA_FAIL = 0, 3


# ── Schema ────────────────────────────────────────────────────────────────────

def live_schema(client, table_id: str) -> list[tuple[str, str, str]]:
    """(name, type, mode) from BigQuery table metadata. Reads no rows."""
    table = client.get_table(table_id)
    return [(f.name, f.field_type, f.mode) for f in table.schema]


def check_schemas(client, project: str, sources: list[str]) -> bool:
    """Log PASS/FAIL per source. Return True if every schema matches."""
    ok = True
    for source in sources:
        table_id = schemas.staging_table(project, source)
        problems = compare_schema(expected_schema(source), live_schema(client, table_id))
        if problems:
            ok = False
            log.error("schema FAIL %s (%d differences)", table_id, len(problems))
            for p in problems:
                log.error("  %s", p)
        else:
            log.info("schema PASS %s (%d columns)", table_id, len(expected_schema(source)))
    return ok


# ── Values (Great Expectations) ───────────────────────────────────────────────

def partition_query(project: str, source: str, ingest_date: dt.date) -> str:
    """The rows the suite validates: needed columns of one ingest partition."""
    cols = ", ".join(columns_for_query(source))
    return (f"SELECT {cols} FROM `{schemas.staging_table(project, source)}` "
            f"WHERE _ingest_date = DATE '{ingest_date.isoformat()}'")


def latest_ingest_date(client, project: str, source: str) -> dt.date | None:
    """Most recent load of a source, from load_log (a small table)."""
    sql = (f"SELECT MAX(ingest_date) AS d FROM `{schemas.load_log_table(project)}` "
           f"WHERE source = @source")
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("source", "STRING", source)])
    rows = list(client.query(sql, job_config=cfg).result())
    return rows[0]["d"] if rows else None


def estimate_bytes(client, sql: str) -> int:
    """Bytes the query would scan (BigQuery dry run: free, nothing executed)."""
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False))
    return job.total_bytes_processed


def run_suite(project: str, source: str, ingest_date: dt.date) -> bool:
    """Validate one partition with the Great Expectations suite. True if all pass."""
    import great_expectations as gx

    context = gx.get_context(mode="ephemeral")
    sources_api = context.data_sources
    add = getattr(sources_api, "add_bigquery", None) or sources_api.add_sql
    datasource = add(name="vigil_bigquery", connection_string=f"bigquery://{project}")
    asset = datasource.add_query_asset(
        name=f"{source}_stg_{ingest_date:%Y%m%d}",
        query=partition_query(project, source, ingest_date))
    batch = asset.add_batch_definition_whole_table("partition").get_batch()

    suite = context.suites.add(build_suite(source, context=context))
    result = batch.validate(suite)

    for r in result.results:
        cfg = r.expectation_config
        col = cfg.kwargs.get("column") or cfg.kwargs.get("column_list")
        res = r.result or {}
        detail = (f"unexpected {res.get('unexpected_count')} "
                  f"({res.get('unexpected_percent') or 0:.2f}%)"
                  if "unexpected_count" in res else f"observed {res.get('observed_value')}")
        level = log.info if r.success else log.error
        level("values %s %s %s %s: %s", "PASS" if r.success else "FAIL",
              source, cfg.type, col, detail)
    log.info("values %s: %d/%d expectations passed (%s, ingest_date %s)",
             source, sum(r.success for r in result.results), len(result.results),
             schemas.staging_table(project, source), ingest_date)
    return bool(result.success)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--source", choices=schemas.SOURCES, help="one source (default: all)")
    ap.add_argument("--values", action="store_true",
                    help="also run the Great Expectations value checks")
    ap.add_argument("--ingest-date", type=dt.date.fromisoformat,
                    help="load to validate (default: latest in load_log)")
    ap.add_argument("--max-gb", type=float, default=2.0,
                    help="stop if the dry run estimates more than this per query")
    ap.add_argument("--estimate-only", action="store_true",
                    help="print the cost estimate and stop")
    args = ap.parse_args()

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    sources = [args.source] if args.source else schemas.SOURCES

    ok = check_schemas(client, args.project, sources)
    if args.values:
        for source in sources:
            day = args.ingest_date or latest_ingest_date(client, args.project, source)
            if day is None:
                log.warning("values SKIP %s: no load in load_log", source)
                continue
            gb = estimate_bytes(client, partition_query(args.project, source, day)) / 1e9
            log.info("values %s: ingest_date %s, about %.3f GB per query", source, day, gb)
            if args.estimate_only:
                continue
            if gb > args.max_gb:
                log.error("values STOP %s: %.3f GB is above --max-gb %.1f", source, gb, args.max_gb)
                ok = False
                continue
            ok = run_suite(args.project, source, day) and ok
    sys.exit(EXIT_OK if ok else EXIT_DATA_FAIL)


if __name__ == "__main__":
    main()
