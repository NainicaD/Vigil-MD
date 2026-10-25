"""Unzip raw GCS files into NDJSON for the BigQuery raw load.

Each output line is {"payload": <record>} with no other change to the record.
The conversion itself lives in scripts/warehouse/convert.py.

  openFDA  zip holds one {"meta": …, "results": [ … ]} JSON file.
  GUDID    zip holds N XML parts. In the daily run, fetch_sources.py already
           converts GUDID during the download (no GCS read-back), so its
           pending entry arrives with content_sha256 and is skipped here.
           --scope all still converts GUDID from GCS.

Content-hash check: the SHA-256 of the canonical records is compared with
the partition state. Same hash → the entry is marked skip_load (nothing to
load, even if the export path is new).

Usage:
    # Daily: files listed in manifests/pending/{source}/ by fetch_sources.py
    python scripts/warehouse/unzip_to_ndjson.py \
        --bucket vigil-md-raw-nd --source maude \
        --scope changed --ingest-date 2026-10-25

    # Full load: every partition in the newest export folder
    python scripts/warehouse/unzip_to_ndjson.py \
        --bucket vigil-md-raw-nd --source maude \
        --scope all --ingest-date 2026-10-25

    # Print the plan only. No GCS call:
    python scripts/warehouse/unzip_to_ndjson.py ... --print-only
"""
from __future__ import annotations

import argparse
import hashlib
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse import common
from scripts.warehouse.convert import (  # noqa: F401  (re-exported for callers)
    Utf8Writer,
    apply_content_check,
    convert_zip,
    data_members,
    gudid_parts,
    xml_to_value,
)
log = get_logger("unzip_to_ndjson")

CHUNK_SIZE = 8 * 1024 * 1024   # same as fetch_sources.py


# ── GCS ───────────────────────────────────────────────────────────────────────

def download_to_temp(bucket, gcs_path: str, tmp) -> str:
    """Copy a GCS blob into a local temp file (zipfile needs seek). Returns SHA-256."""
    sha = hashlib.sha256()
    with bucket.blob(gcs_path).open("rb") as src:
        while chunk := src.read(CHUNK_SIZE):
            tmp.write(chunk)
            sha.update(chunk)
    tmp.flush()
    tmp.seek(0)
    return sha.hexdigest()


def process_entry(bucket, source: str, ingest_date: str, entry: dict,
                  part_state: dict) -> bool:
    """Unzip one entry to NDJSON in GCS. Returns True if it must load."""
    ndjson_name = common.ndjson_path(source, ingest_date, entry["partition_key"])
    with tempfile.TemporaryFile() as tmp:
        zip_sha = download_to_temp(bucket, entry["gcs_path"], tmp)
        if entry.get("zip_sha256") and entry["zip_sha256"] != zip_sha:
            raise RuntimeError(
                f"{entry['gcs_path']}: SHA-256 differs from the pending entry "
                f"({zip_sha[:12]}… vs {entry['zip_sha256'][:12]}…)")
        entry["zip_sha256"] = zip_sha
        # "wb" + Utf8Writer: an error cancels the upload ("wt" would finalize it)
        with bucket.blob(ndjson_name).open("wb") as raw:
            rows, content_sha = convert_zip(tmp, Utf8Writer(raw), source)

    must_load = apply_content_check(
        entry, rows, content_sha, ndjson_name, part_state.get(entry["partition_key"]))
    if must_load:
        log.info("  %s: %d rows → gs://%s/%s",
                 entry["partition_key"], rows, bucket.name, ndjson_name)
    else:
        # Same records as last load: refresh pointers only (never content_sha256)
        st = part_state[entry["partition_key"]]
        for k in ("zip_sha256", "gcs_path", "export_date", "records", "size_mb"):
            if entry.get(k) is not None:
                st[k] = entry[k]
        log.info("  %s: content unchanged (%d rows) → skip_load",
                 entry["partition_key"], rows)
    return must_load


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket",      required=True)
    parser.add_argument("--source",      required=True, choices=common.SOURCES)
    parser.add_argument("--scope",       default="changed", choices=["changed", "all"])
    parser.add_argument("--ingest-date", required=True,
                        type=lambda s: date.fromisoformat(s).isoformat(),
                        help="YYYY-MM-DD; names the ndjson/ folder and raw partition")
    parser.add_argument("--print-only",  action="store_true",
                        help="Print the plan, then exit. No GCS calls.")
    args = parser.parse_args()

    endpoint = common.ENDPOINTS[args.source]
    if args.print_only:
        print(f"=== unzip_to_ndjson plan: {args.source} (scope={args.scope}) ===")
        if args.scope == "changed":
            print(f"  input:  entries without content_sha256 in "
                  f"gs://{args.bucket}/manifests/pending/{args.source}/*.json")
        else:
            print(f"  input:  every .zip in the newest folder of "
                  f"gs://{args.bucket}/raw/{endpoint}/<export_date>/")
            print(f"  writes: gs://{args.bucket}/manifests/pending/"
                  f"{args.source}/full_<run_ts>.json")
        print(f"  output: gs://{args.bucket}/"
              f"{common.ndjson_path(args.source, args.ingest_date, '<partition_key>')}")
        print("  line:   {\"payload\": <record>}")
        print("  check:  content_sha256 == partition state → skip_load")
        return

    from google.cloud import storage
    bucket = storage.Client().bucket(args.bucket)
    part_state = common.load_partition_state(bucket, args.source) or {}

    if args.scope == "all":
        entries = common.list_all_latest(bucket, args.source)
        if not entries:
            raise RuntimeError(f"No zip files under raw/{endpoint}/ for --scope all")
        run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        manifests = [(f"{common.PENDING_PREFIX}/{args.source}/full_{run_ts}.json",
                      entries)]
    else:
        manifests = common.read_pending(bucket, args.source)
        if not manifests:
            log.info("%s: nothing pending", args.source)
            return

    to_load = skipped = done_before = 0
    for blob_name, entries in manifests:
        log.info("── %s (%d entries) ──", blob_name, len(entries))
        for entry in entries:
            # Already converted: by an earlier run, or by the GUDID fetch
            if "content_sha256" in entry:
                done_before += 1
                continue
            if process_entry(bucket, args.source, args.ingest_date, entry, part_state):
                to_load += 1
            else:
                skipped += 1
            common.write_pending(bucket, blob_name, entries)   # save progress per file

    if skipped:
        common.save_partition_state(bucket, args.source, part_state)

    print(f"\n=== {args.source}: {to_load} to load, {skipped} unchanged (skip_load), "
          f"{done_before} already converted ===")


if __name__ == "__main__":
    main()
