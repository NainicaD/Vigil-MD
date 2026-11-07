"""Build the MAUDE event tables (problem terms and narrative text) for one ingest date.

Runs after the MAUDE merge. Three steps, one per Airflow task:

  --step staging   RAW maude_raw (one scan of the @ingest_date partition)
                   → vigil_md_staging.maude_events_stg, one row per report:
                   event_type, dates, product_problems[], patient_problems[],
                   mdr_text (JSON array). Replaces that day's staging rows.
                   Partitions expire after 14 days.
  --step problems  staging → vigil_md_prod.maude_problems, one row per
                   (report, problem_type 'device'|'patient', problem_term)
  --step text      staging → vigil_md_prod.maude_text, one row per narrative
                   text, filtered (scripts/warehouse/maude_events_config.py)

The prod steps replace each report's rows: in one transaction they delete the
rows of every report in the day's staging partition, insert the new rows, and
roll back if a key is duplicated. A report whose problem list or text changed
stays correct. The delete is pruned to the months (_part_date) of the batch,
plus the 1900-01-01 partition for reports without a date_received.

Cost guard: every step is dry-run first (free). If it would scan more than
--max-gb, nothing runs (exit 2). The job also gets maximum_bytes_billed.

Usage:
    python scripts/warehouse/build_maude_events.py --project vigil-md-1792696816 \
        --ingest-date 2026-11-03 --step staging

    # Print the SQL only. No GCP calls:
    python scripts/warehouse/build_maude_events.py ... --print-only
    # Dry run only (free): bytes each query would scan:
    python scripts/warehouse/build_maude_events.py ... --estimate-only
    # Text step: count the rows and MB the filter would keep (reads staging):
    python scripts/warehouse/build_maude_events.py ... --step text --count-only
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import schemas  # noqa: E402
from scripts.warehouse.build_staging import (  # noqa: E402  (same date rule as staging_maude)
    FUNCTIONS, RAW_TOKEN, render_raw_count_sql)
from scripts.warehouse.maude_events_config import TEXT_FILTER  # noqa: E402

log = get_logger("build_maude_events")

SQL_FILE = Path(__file__).resolve().parent / "sql" / "staging_maude_events.sql"
STEPS = ["staging", "problems", "text"]
DEFAULT_MAX_GB = {"staging": 60.0, "problems": 10.0, "text": 40.0}   # full export: 48.8 / 2.7 / 32.3 GB (2026-11-07)
SENTINEL = f"DATE '{schemas.PART_DATE_SENTINEL}'"
EXIT_OVER_BUDGET = 2

STG_COLS = [n for n, _, _ in schemas.MAUDE_EVENTS_STAGING_COLUMNS]
BIZ_COLS = [c for c in STG_COLS if c not in ("record_hash", "_ingest_date", "_loaded_at")]


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def read_events_sql() -> str:
    return SQL_FILE.read_text()


def validate_events_sql(sql: str) -> None:
    """Raise unless the file selects every staging column and filters the partition."""
    if RAW_TOKEN not in sql:
        raise ValueError(f"{SQL_FILE.name}: no {RAW_TOKEN}")
    if not re.search(r"_ingest_date\s*=\s*@ingest_date", sql):
        raise ValueError(f"{SQL_FILE.name}: no '_ingest_date = @ingest_date' filter")
    body = re.sub(r"--[^\n]*", "", sql)
    wanted = BIZ_COLS + ["_ingest_date", "_loaded_at"]
    missing = [c for c in wanted
               if not re.search(rf"(\bAS\s+{c}\b|^\s*(\w+\.)?{c}\s*,?\s*$)", body, re.M)]
    if missing:
        raise ValueError(f"{SQL_FILE.name}: columns not selected: {missing}")


def render_staging_sql(raw: str, stg: str, source_sql: str) -> str:
    """Replace the staging rows of @ingest_date: one row per report. Pure function."""
    validate_events_sql(source_sql)
    select_sql = source_sql.replace(RAW_TOKEN, raw).strip().rstrip(";")
    return f"""\
{FUNCTIONS}
BEGIN TRANSACTION;

DELETE FROM `{stg}` WHERE _ingest_date = @ingest_date;

INSERT INTO `{stg}` ({", ".join(STG_COLS)})
WITH src AS (
{select_sql}
)
SELECT {", ".join(STG_COLS)}
FROM (
  SELECT *, TO_HEX(SHA256(TO_JSON_STRING(STRUCT({", ".join(BIZ_COLS)})))) AS record_hash
  FROM src
)
WHERE mdr_report_key IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY mdr_report_key ORDER BY _loaded_at DESC, record_hash) = 1;

COMMIT TRANSACTION;

SELECT
  COUNT(*)                                       AS staged_reports,
  COUNTIF(ARRAY_LENGTH(product_problems) > 0)    AS with_product_problems,
  COUNTIF(ARRAY_LENGTH(patient_problems) > 0)    AS with_patient_problems
FROM `{stg}` WHERE _ingest_date = @ingest_date;
"""


def render_bounds_sql(stg: str) -> str:
    """Reports in the staging partition and their date_received range."""
    return f"""\
SELECT COUNT(*) AS n, MIN(date_received) AS min_d, MAX(date_received) AS max_d
FROM `{stg}` WHERE _ingest_date = @ingest_date"""


def _in_batch_months(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return f"({p}_part_date BETWEEN @min_d AND @max_d OR {p}_part_date = {SENTINEL})"


def _replace_script(name: str, prod: str, stg: str, insert_sql: str) -> str:
    """Delete the batch's reports, insert their new rows, check keys. Pure function."""
    keys = ", ".join(schemas.MAUDE_EVENT_KEYS[name])
    return f"""\
DECLARE deleted INT64;
DECLARE inserted INT64;
DECLARE dup_keys INT64;

BEGIN TRANSACTION;

DELETE FROM `{prod}`
WHERE {_in_batch_months()}
  AND mdr_report_key IN (SELECT mdr_report_key FROM `{stg}` WHERE _ingest_date = @ingest_date);
SET deleted = @@row_count;

{insert_sql.strip().rstrip(";")};
SET inserted = @@row_count;

-- Rows outside the batch's months are not touched, so this range is enough.
SET dup_keys = (
  SELECT COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys})))
  FROM `{prod}` WHERE {_in_batch_months()});
IF dup_keys > 0 THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('{prod}: %d duplicate keys after the write. Rolled back.', dup_keys);
END IF;

COMMIT TRANSACTION;

SELECT deleted AS rows_deleted, inserted AS rows_inserted, dup_keys;
"""


def render_problems_sql(prod: str, stg: str) -> str:
    cols = [n for n, _, _ in schemas.MAUDE_PROBLEMS_COLUMNS]
    parts = []
    for problem_type, column in (("device", "product_problems"), ("patient", "patient_problems")):
        parts.append(f"""\
SELECT s.mdr_report_key, '{problem_type}', term, s.event_type, s.date_received,
       COALESCE(s.date_received, {SENTINEL}), CURRENT_TIMESTAMP()
FROM `{stg}` AS s, UNNEST(s.{column}) AS term
WHERE s._ingest_date = @ingest_date""")
    insert = f"INSERT INTO `{prod}` ({', '.join(cols)})\n" + "\nUNION ALL\n".join(parts)
    return _replace_script("maude_problems", prod, stg, insert)


def text_select_sql(stg: str, filt: dict) -> str:
    """The filtered, cleaned text rows of @ingest_date. Pure function.

    filt: text_types / event_types (a list, or None = no filter), from_year, to_year.
    Cleaning: trim, drop control characters (keep tab and newline), drop empty
    text, one row per (report, mdr_text_key). BigQuery strings are already valid
    UTF-8 (the RAW load parses them), so no decoding step is needed here.
    """
    report_filters = ["s._ingest_date = @ingest_date",
                      "s.date_received BETWEEN @from_date AND @to_date"]
    if filt.get("event_types") is not None:
        report_filters.append("s.event_type IN UNNEST(@event_types)")
    text_filters = ["text IS NOT NULL"]
    if filt.get("text_types") is not None:
        text_filters.append("text_type_code IN UNNEST(@text_types)")
    return f"""\
SELECT mdr_report_key, mdr_text_key, text_type_code, patient_sequence_number,
       event_type, date_received, date_report, text, _part_date
FROM (
  SELECT
    s.mdr_report_key,
    NULLIF(TRIM(JSON_VALUE(t, '$.mdr_text_key')), '')            AS mdr_text_key,
    NULLIF(TRIM(JSON_VALUE(t, '$.text_type_code')), '')          AS text_type_code,
    NULLIF(TRIM(JSON_VALUE(t, '$.patient_sequence_number')), '') AS patient_sequence_number,
    s.event_type, s.date_received, s.date_report,
    NULLIF(TRIM(REGEXP_REPLACE(JSON_VALUE(t, '$.text'),
                               r'[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F]', '')), '') AS text,
    COALESCE(s.date_received, {SENTINEL})                       AS _part_date
  FROM `{stg}` AS s, UNNEST(JSON_QUERY_ARRAY(s.mdr_text)) AS t
  WHERE {" AND ".join(report_filters)}
)
WHERE {" AND ".join(text_filters)}
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY mdr_report_key, mdr_text_key ORDER BY LENGTH(text) DESC, text) = 1"""


def render_text_sql(prod: str, stg: str, filt: dict) -> str:
    cols = [n for n, _, _ in schemas.MAUDE_TEXT_COLUMNS]
    select_cols = [c for c in cols if c != "last_updated_at"]
    insert = (f"INSERT INTO `{prod}` ({', '.join(cols)})\n"
              f"SELECT {', '.join(select_cols)}, CURRENT_TIMESTAMP()\n"
              f"FROM (\n{text_select_sql(stg, filt)}\n)")
    return _replace_script("maude_text", prod, stg, insert)


def render_text_count_sql(stg: str, filt: dict) -> str:
    return f"""\
SELECT COUNT(*) AS text_rows, COUNT(DISTINCT mdr_report_key) AS reports,
       ROUND(SUM(BYTE_LENGTH(text)) / 1e6, 1) AS text_mb
FROM (
{text_select_sql(stg, filt)}
)"""


def staging_would_expire(ingest_date: str, today: date) -> bool:
    """True if the staging partition for ingest_date is already past its expiry."""
    age = (today - date.fromisoformat(ingest_date)).days
    return age >= schemas.MAUDE_EVENTS_STAGING_EXPIRY_DAYS


def parse_filter(args) -> dict:
    """TEXT_FILTER with the command-line overrides. 'all' = no filter. Pure function."""
    filt = dict(TEXT_FILTER)
    for key in ("text_types", "event_types"):
        value = getattr(args, key)
        if value is not None:
            filt[key] = None if value == ["all"] else value
    for key in ("from_year", "to_year"):
        if getattr(args, key) is not None:
            filt[key] = getattr(args, key)
    if filt["from_year"] > filt["to_year"]:
        raise ValueError(f"from_year {filt['from_year']} is after to_year {filt['to_year']}")
    return filt


# ── BigQuery ──────────────────────────────────────────────────────────────────

def query_params(ingest_date: str, bounds=None, filt: dict | None = None) -> list:
    from google.cloud import bigquery
    params = [bigquery.ScalarQueryParameter("ingest_date", "DATE", ingest_date)]
    if bounds is not None:
        params += [bigquery.ScalarQueryParameter("min_d", "DATE", bounds[0]),
                   bigquery.ScalarQueryParameter("max_d", "DATE", bounds[1])]
    if filt is not None:
        params += [bigquery.ScalarQueryParameter("from_date", "DATE", f"{filt['from_year']}-01-01"),
                   bigquery.ScalarQueryParameter("to_date", "DATE", f"{filt['to_year']}-12-31")]
        for key in ("text_types", "event_types"):
            if filt.get(key) is not None:
                params.append(bigquery.ArrayQueryParameter(key, "STRING", filt[key]))
    return params


def dry_run_gb(client, sql: str, params: list) -> float:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=params))
    return job.total_bytes_processed / 1e9


def run_capped(client, sql: str, params: list, max_gb: float, label: str):
    """Dry-run, refuse above max_gb, then run with maximum_bytes_billed."""
    from google.cloud import bigquery
    gb = dry_run_gb(client, sql, params)
    if gb > max_gb:
        log.error("STOP %s: dry run %.2f GB is above --max-gb %.1f; nothing ran", label, gb, max_gb)
        raise SystemExit(EXIT_OVER_BUDGET)
    log.info("%s: dry run %.2f GB (cap %.1f GB)", label, gb, max_gb)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=int(max_gb * 1e9)))
    return next(iter(job.result()))


def batch_bounds(client, stg: str, ingest_date: str):
    """(n, (min_d, max_d)) of the staging partition. Sentinel range if no dates."""
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(query_parameters=query_params(ingest_date))
    b = next(iter(client.query(render_bounds_sql(stg), job_config=cfg).result()))
    lo = b.min_d.isoformat() if b.min_d else schemas.PART_DATE_SENTINEL
    hi = b.max_d.isoformat() if b.max_d else schemas.PART_DATE_SENTINEL
    return b.n, (lo, hi)


# ── Main ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project",     required=True)
    p.add_argument("--ingest-date", required=True,
                   type=lambda s: date.fromisoformat(s).isoformat())
    p.add_argument("--step",        required=True, choices=STEPS)
    p.add_argument("--max-gb",      type=float, default=None,
                   help=f"Refuse to run above this scan size. Defaults: {DEFAULT_MAX_GB}")
    p.add_argument("--print-only",  action="store_true", help="Print the SQL. No GCP calls.")
    p.add_argument("--estimate-only", action="store_true", help="Dry run only (free).")
    p.add_argument("--count-only",  action="store_true",
                   help="Text step: count the rows and MB the filter keeps. Writes nothing.")
    p.add_argument("--text-types",  nargs="+", default=None,
                   help="Text step: text_type_code values, or 'all'")
    p.add_argument("--event-types", nargs="+", default=None,
                   help="Text step: event_type values, or 'all'")
    p.add_argument("--from-year",   type=int, default=None)
    p.add_argument("--to-year",     type=int, default=None)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.count_only and args.step != "text":
        raise SystemExit("--count-only is for --step text")
    max_gb = args.max_gb if args.max_gb is not None else DEFAULT_MAX_GB[args.step]
    filt = parse_filter(args)

    raw = schemas.raw_table(args.project, "maude")
    stg = schemas.maude_events_staging_table(args.project)
    prod = {"problems": schemas.maude_event_prod_table(args.project, "maude_problems"),
            "text":     schemas.maude_event_prod_table(args.project, "maude_text")}
    for t in (stg, *prod.values()):
        schemas.assert_not_protected(t)

    if args.step == "staging":
        sql = render_staging_sql(raw, stg, read_events_sql())
    elif args.step == "problems":
        sql = render_problems_sql(prod["problems"], stg)
    else:
        sql = render_text_sql(prod["text"], stg, filt)

    if args.print_only:
        print(f"-- build_maude_events: step {args.step}, @ingest_date = {args.ingest_date}")
        if args.step == "text":
            print(f"-- filter: {filt}")
            print("-- count (--count-only):\n" + render_text_count_sql(stg, filt) + "\n")
        if args.step != "staging":
            print("-- 1. batch bounds:\n" + render_bounds_sql(stg) + "\n")
        print("-- script:\n" + sql)
        return

    if args.step == "staging" and staging_would_expire(args.ingest_date, date.today()):
        log.error("Ingest date %s is %d+ days old: its maude_events_stg partition would "
                  "expire at once. Build staging for a recent ingest date.",
                  args.ingest_date, schemas.MAUDE_EVENTS_STAGING_EXPIRY_DAYS)
        raise SystemExit(1)

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)

    if args.step == "staging":
        params = query_params(args.ingest_date)
        if args.estimate_only:
            print(f"staging: dry run {dry_run_gb(client, sql, params):.2f} GB (cap {max_gb} GB)")
            return
        n_raw = next(iter(client.query(render_raw_count_sql(raw),
                                       job_config=bigquery.QueryJobConfig(query_parameters=params)).result())).n
        if n_raw == 0:
            log.info("maude raw partition %s has no rows — nothing to stage", args.ingest_date)
            return
        row = run_capped(client, sql, params, max_gb, "staging")
        if row.staged_reports == 0:
            log.error("%d raw rows but 0 staged reports for %s (expired partition?)",
                      n_raw, args.ingest_date)
            raise SystemExit(1)
        print(f"\n=== maude_events {args.ingest_date}: {n_raw} raw rows → {row.staged_reports} "
              f"reports staged ({row.with_product_problems} with device problems, "
              f"{row.with_patient_problems} with patient problems) ===")
        return

    n, bounds = batch_bounds(client, stg, args.ingest_date)
    if n == 0:
        log.info("maude_events_stg partition %s is empty — nothing to write", args.ingest_date)
        return
    log.info("%d staged reports, date_received %s … %s", n, *bounds)
    params = query_params(args.ingest_date, bounds,
                          filt if args.step == "text" else None)

    if args.count_only:
        row = run_capped(client, render_text_count_sql(stg, filt), params, max_gb, "text count")
        print(f"\n=== maude_text filter {filt}: {row.text_rows} texts from {row.reports} "
              f"reports, {row.text_mb} MB of text ===")
        return
    if args.estimate_only:
        print(f"{args.step}: dry run {dry_run_gb(client, sql, params):.2f} GB (cap {max_gb} GB)")
        return

    row = run_capped(client, sql, params, max_gb, args.step)
    table = client.get_table(prod[args.step])
    print(f"\n=== {args.step} {args.ingest_date}: {n} reports → {row.rows_deleted} rows "
          f"deleted, {row.rows_inserted} inserted (0 duplicate keys). {table.table_id}: "
          f"{table.num_rows} rows, {table.num_bytes / 1e9:.2f} GB ===")


if __name__ == "__main__":
    main()
