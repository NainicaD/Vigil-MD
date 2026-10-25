"""Load NDJSON files into the RAW BigQuery tables, idempotently.

Input: pending manifests in gs://<bucket>/manifests/pending/{source}/.
  --scope changed   daily manifests from fetch_sources.py  ({run_ts}.json)
  --scope all       full-load manifests from unzip --scope all (full_{run_ts}.json)

Per pending entry (skip_load entries need no load):
  1. load_log already has (source_file, content_sha256) → skip ("already loaded")
  2. load the NDJSON into a temporary table in vigil_md_ops (explicit schema)
  3. one BigQuery script:
       row count check (raises before any write)
       BEGIN TRANSACTION
         DELETE raw rows for (_ingest_date, _source_file)
         INSERT the new rows
         INSERT one load_log row
       COMMIT TRANSACTION
  4. drop the temporary table (finally)
  5. write the hashes to the partition state (only after the commit)
A manifest is deleted when every entry in it is done.

Running the same ingest date again replaces that file's rows, never adds a
second copy.

Usage:
    python scripts/warehouse/load_raw_to_bq.py \
        --project vigil-md-1792696816 --bucket vigil-md-raw-nd \
        --source maude --scope changed --ingest-date 2026-10-25

    # Print the SQL and the plan only. No GCP calls:
    python scripts/warehouse/load_raw_to_bq.py ... --print-only
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse import common, schemas
log = get_logger("load_raw_to_bq")

TMP_EXPIRY = timedelta(days=1)    # safety net if the finally-drop fails


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def render_already_loaded_sql(load_log: str) -> str:
    return (
        f"SELECT COUNT(*) AS n FROM `{load_log}`\n"
        "WHERE source = @source AND source_file = @source_file\n"
        "  AND content_sha256 = @content_sha256")


def render_load_sql(raw: str, tmp: str, load_log: str) -> str:
    """The row-count check and the transaction, as one BigQuery script."""
    return f"""\
DECLARE n INT64 DEFAULT (SELECT COUNT(*) FROM `{tmp}`);
IF n != @expected_rows THEN
  RAISE USING MESSAGE = FORMAT('%s: %d rows in NDJSON, pending entry says %d',
                               @source_file, n, @expected_rows);
END IF;

BEGIN TRANSACTION;

DELETE FROM `{raw}`
WHERE _ingest_date = @ingest_date AND _source_file = @source_file;

INSERT INTO `{raw}`
  (_ingest_date, _source_file, _source_partition, _zip_sha256,
   _content_sha256, _loaded_at, payload)
SELECT @ingest_date, @source_file, @source_partition, @zip_sha256,
       @content_sha256, CURRENT_TIMESTAMP(), payload
FROM `{tmp}`;

INSERT INTO `{load_log}`
  (source, source_file, zip_sha256, content_sha256, ingest_date,
   row_count, mode, loaded_at)
VALUES (@source, @source_file, @zip_sha256, @content_sha256, @ingest_date,
        n, @mode, CURRENT_TIMESTAMP());

COMMIT TRANSACTION;
"""


def tmp_table_name(project: str, source: str, run_ts: str, i: int) -> str:
    table_id = f"{project}.{schemas.DATASETS['ops']}._tmp_{source}_{run_ts}_{i}"
    schemas.assert_not_protected(table_id)
    return table_id


# ── Entry selection and state (pure) ──────────────────────────────────────────

def manifest_in_scope(blob_name: str, scope: str) -> bool:
    is_full = blob_name.rsplit("/", 1)[-1].startswith("full_")
    return is_full if scope == "all" else not is_full


def check_entry(entry: dict) -> None:
    """Raise unless the entry was converted to NDJSON first."""
    missing = [k for k in ("content_sha256", "row_count", "ndjson_path")
               if entry.get(k) in (None, "")]
    if missing and not entry.get("skip_load"):
        raise RuntimeError(
            f"{entry.get('gcs_path')}: missing {missing}. "
            "Run unzip_to_ndjson.py for this source first.")
    if not entry.get("skip_load") and int(entry["row_count"]) <= 0:
        raise RuntimeError(f"{entry.get('gcs_path')}: row_count is 0")


def state_after_load(entry: dict, loaded_at: str) -> dict:
    """Partition state fields written after the commit."""
    return {
        "gcs_path":       entry["gcs_path"],
        "export_date":    entry.get("export_date"),
        "records":        entry.get("records"),
        "size_mb":        entry.get("size_mb"),
        "zip_sha256":     entry.get("zip_sha256"),
        "content_sha256": entry["content_sha256"],
        "loaded_at":      loaded_at,
    }


def query_params(entry: dict, source: str, ingest_date: str, mode: str) -> list[tuple]:
    """(name, type, value) for every script parameter. Pure function."""
    return [
        ("ingest_date",      "DATE",   ingest_date),
        ("source",           "STRING", source),
        ("source_file",      "STRING", entry["gcs_path"]),
        ("source_partition", "STRING", entry.get("partition_key")),
        ("zip_sha256",       "STRING", entry.get("zip_sha256")),
        ("content_sha256",   "STRING", entry["content_sha256"]),
        ("expected_rows",    "INT64",  int(entry["row_count"])),
        ("mode",             "STRING", mode),
    ]


# ── BigQuery ──────────────────────────────────────────────────────────────────

def _job_config(params: list[tuple]):
    from google.cloud import bigquery
    return bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter(n, t, v) for n, t, v in params])


def already_loaded(client, load_log: str, entry: dict, source: str) -> bool:
    params = [("source", "STRING", source),
              ("source_file", "STRING", entry["gcs_path"]),
              ("content_sha256", "STRING", entry["content_sha256"])]
    rows = client.query(render_already_loaded_sql(load_log),
                        job_config=_job_config(params)).result()
    return next(iter(rows)).n > 0


def load_tmp(client, bucket_name: str, entry: dict, tmp: str) -> None:
    from google.cloud import bigquery
    table = bigquery.Table(tmp, schema=[bigquery.SchemaField("payload", "JSON")])
    table.expires = datetime.now(timezone.utc) + TMP_EXPIRY
    client.create_table(table, exists_ok=True)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=[bigquery.SchemaField("payload", "JSON")],
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        max_bad_records=0,
    )
    uri = f"gs://{bucket_name}/{entry['ndjson_path']}"
    log.info("  load %s → %s", uri, tmp)
    client.load_table_from_uri(uri, tmp, job_config=job_config).result()


def load_entry(client, bucket_name: str, project: str, source: str,
               ingest_date: str, mode: str, entry: dict, tmp: str) -> None:
    raw = schemas.raw_table(project, source)
    load_log = schemas.load_log_table(project)
    try:
        load_tmp(client, bucket_name, entry, tmp)
        sql = render_load_sql(raw, tmp, load_log)
        client.query(sql, job_config=_job_config(
            query_params(entry, source, ingest_date, mode))).result()
    finally:
        client.delete_table(tmp, not_found_ok=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project",     required=True)
    parser.add_argument("--bucket",      required=True)
    parser.add_argument("--source",      required=True, choices=common.SOURCES)
    parser.add_argument("--scope",       default="changed", choices=["changed", "all"])
    parser.add_argument("--ingest-date", required=True,
                        type=lambda s: date.fromisoformat(s).isoformat())
    parser.add_argument("--mode",        default="incremental",
                        choices=["incremental", "full"],
                        help="Written to load_log.mode")
    parser.add_argument("--print-only",  action="store_true",
                        help="Print the SQL and the plan, then exit. No GCP calls.")
    args = parser.parse_args()

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw = schemas.raw_table(args.project, args.source)
    load_log = schemas.load_log_table(args.project)

    if args.print_only:
        tmp = tmp_table_name(args.project, args.source, run_ts, 0)
        kind = "full_*.json" if args.scope == "all" else "{run_ts}.json (not full_*)"
        print(f"=== load_raw_to_bq plan: {args.source} (scope={args.scope}, "
              f"mode={args.mode}, ingest_date={args.ingest_date}) ===")
        print(f"  input:   gs://{args.bucket}/manifests/pending/{args.source}/{kind}")
        print(f"  target:  {raw}  partition _ingest_date={args.ingest_date}")
        print(f"  temp:    {tmp}  (expires in {TMP_EXPIRY}, dropped in finally)")
        print("\n-- 1. already loaded?\n" + render_already_loaded_sql(load_log))
        print("\n-- 2. load NDJSON → temp table (schema: payload JSON, WRITE_TRUNCATE)")
        print("\n-- 3. script\n" + render_load_sql(raw, tmp, load_log))
        print("-- parameters (example entry)")
        example = {"gcs_path": "raw/event/2026-10-20/2024q1/device-event-0001-of-0004.json.zip",
                   "partition_key": "2024q1/device-event-0001-of-0004.json.zip",
                   "zip_sha256": "<zip sha>", "content_sha256": "<content sha>",
                   "row_count": 123456}
        for n, t, v in query_params(example, args.source, args.ingest_date, args.mode):
            print(f"--   @{n:<17} {t:<7} {v}")
        return

    from google.cloud import bigquery, storage
    bucket = storage.Client().bucket(args.bucket)
    client = bigquery.Client(project=args.project)

    manifests = [(b, e) for b, e in common.read_pending(bucket, args.source)
                 if manifest_in_scope(b, args.scope)]
    if not manifests:
        log.info("%s: nothing pending (scope=%s)", args.source, args.scope)
        return

    part_state = common.load_partition_state(bucket, args.source) or {}
    loaded = skipped = already = 0
    i = 0
    for blob_name, entries in manifests:
        log.info("── %s (%d entries) ──", blob_name, len(entries))
        for entry in entries:
            check_entry(entry)
            if entry.get("skip_load"):
                skipped += 1
                continue
            key = entry["partition_key"]
            if already_loaded(client, load_log, entry, args.source):
                log.info("  already loaded: %s", entry["gcs_path"])
                already += 1
            else:
                tmp = tmp_table_name(args.project, args.source, run_ts, i)
                i += 1
                load_entry(client, args.bucket, args.project, args.source,
                           args.ingest_date, args.mode, entry, tmp)
                log.info("  committed %s rows: %s", entry["row_count"], entry["gcs_path"])
                loaded += 1
            # After the commit (or a commit by an earlier run): save the hashes
            part_state[key] = {**part_state.get(key, {}),
                               **state_after_load(entry, datetime.now(timezone.utc).isoformat())}
            common.save_partition_state(bucket, args.source, part_state)
        bucket.blob(blob_name).delete()
        log.info("  done, deleted %s", blob_name)

    print(f"\n=== {args.source}: {loaded} loaded, {already} already loaded, "
          f"{skipped} skip_load (unchanged) → {raw} ${args.ingest_date} ===")


if __name__ == "__main__":
    main()
