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

Changed reports only (MAUDE, scope changed; scripts/warehouse/report_delta.py):
each report is fingerprinted; reports with the same fingerprint as in PROD are
not written, so a re-export loads only new or changed reports. The file's
fingerprints go to a pending shard, promoted after the RAW load commits (or
here, when nothing in the file changed). --scope all writes every report (the
full load rebuilds PROD from staging) and also writes the shards.

Several pending exports (scope changed): only the newest export_date of each
partition is kept; older entries are removed from their manifest, and a manifest
left empty is deleted. Both versions would write the same NDJSON path.

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
from scripts.warehouse import report_delta
from scripts.warehouse.convert import (  # noqa: F401  (re-exported for callers)
    convert_zip_gz,
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


class QuarterLookups:
    """Fingerprint lookup of one quarter at a time (a quarter is ~1M reports)."""

    def __init__(self, bucket, source: str, use_store: bool = True):
        self.bucket, self.source, self.use_store = bucket, source, use_store
        self.qtr, self.lookup = None, {}

    def get(self, partition_key: str) -> dict:
        if not self.use_store:
            return {}                    # full load: every report is written
        qtr = report_delta.quarter(partition_key)
        if qtr != self.qtr:
            self.qtr, self.lookup = qtr, report_delta.load_lookup(self.bucket, self.source, qtr)
            log.info("  fingerprints %s: %d reports known", qtr, len(self.lookup))
        return self.lookup


def process_entry(bucket, source: str, ingest_date: str, entry: dict,
                  part_state: dict, lookups: QuarterLookups | None = None) -> bool:
    """Unzip one entry to NDJSON in GCS. Returns True if it must load."""
    key = entry["partition_key"]
    ndjson_name = common.ndjson_path(source, ingest_date, key)
    delta = (report_delta.ReportDelta(source, lookups.get(key))
             if lookups is not None and source in report_delta.DELTA_SOURCES else None)
    with tempfile.TemporaryFile() as tmp:
        zip_sha = download_to_temp(bucket, entry["gcs_path"], tmp)
        if entry.get("zip_sha256") and entry["zip_sha256"] != zip_sha:
            raise RuntimeError(
                f"{entry['gcs_path']}: SHA-256 differs from the pending entry "
                f"({zip_sha[:12]}… vs {entry['zip_sha256'][:12]}…)")
        entry["zip_sha256"] = zip_sha
        from google.cloud.storage.retry import DEFAULT_RETRY  # lazy: --print-only must not import GCP
        # "wb" + gzip: an error cancels the upload ("wt" would finalize it)
        with bucket.blob(ndjson_name).open(
            "wb",
            chunk_size=8 * 1024 * 1024,              # 8 MB chunks: ~10 s each at home upload speed
            timeout=300,                             # per-request socket timeout
            retry=DEFAULT_RETRY.with_timeout(900),   # keep retrying a chunk for up to 15 min
        ) as raw:
            rows, content_sha = convert_zip_gz(tmp, raw, source,
                                               delta.keep if delta else None)

    must_load = apply_content_check(
        entry, rows, content_sha, ndjson_name, part_state.get(entry["partition_key"]))
    if delta:
        report_delta.write_pending(bucket, source, ingest_date, key, delta.fps)
        entry["delta"] = delta.summary()
        if must_load:
            entry["row_count"] = delta.written       # the NDJSON holds only these
            if delta.written == 0:
                entry["skip_load"] = True           # nothing new: no load
                must_load = False
        log.info("  %s: %d reports, %d new, %d changed, %d unchanged (not written)",
                 key, delta.records, delta.new, delta.changed, delta.records - delta.written)
        if not must_load:
            report_delta.promote(bucket, source, ingest_date, key)   # nothing to load
    if must_load:
        log.info("  %s: %d rows → gs://%s/%s",
                 entry["partition_key"], entry["row_count"], bucket.name, ndjson_name)
    else:
        # Same records as last load: refresh pointers only (never content_sha256)
        st = part_state.setdefault(entry["partition_key"], {})   # a new file of known reports
        for k in ("zip_sha256", "gcs_path", "export_date", "records", "size_mb"):
            if entry.get(k) is not None:
                st[k] = entry[k]
        log.info("  %s: nothing new (%d rows) → skip_load",
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
        if args.source in report_delta.DELTA_SOURCES:
            print("  delta:  " + ("only new or changed reports are written (fingerprints in "
                                  f"gs://{args.bucket}/{report_delta.PREFIX}/{args.source}/)"
                                  if args.scope == "changed" else
                                  "every report is written (full load); fingerprints are saved"))
        return

    from google.cloud import storage
    bucket = storage.Client().bucket(args.bucket)
    part_state = common.load_partition_state(bucket, args.source) or {}
    dropped: list[dict] = []

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
        # Several exports pending (a delayed load): only the newest version of each
        # partition is converted and loaded (docs/issue.md #15)
        sizes = {name: len(entries) for name, entries in manifests}
        manifests, dropped = common.drop_superseded(manifests)
        for e in dropped:
            log.info("  superseded by a newer export, not loaded: %s", e["gcs_path"])
        for name, entries in manifests:
            if len(entries) == sizes[name]:
                continue
            if entries:
                common.write_pending(bucket, name, entries)
            else:
                bucket.blob(name).delete()
                log.info("  deleted %s (every entry superseded)", name)
        manifests = [(name, entries) for name, entries in manifests if entries]

    lookups = QuarterLookups(bucket, args.source, use_store=(args.scope == "changed"))
    to_load = skipped = done_before = 0
    for blob_name, entries in manifests:
        log.info("── %s (%d entries) ──", blob_name, len(entries))
        for entry in entries:
            # Already converted: by an earlier run, or by the GUDID fetch
            if "content_sha256" in entry:
                done_before += 1
                continue
            if process_entry(bucket, args.source, args.ingest_date, entry, part_state, lookups):
                to_load += 1
            else:
                skipped += 1
            common.write_pending(bucket, blob_name, entries)   # save progress per file

    if skipped:
        common.save_partition_state(bucket, args.source, part_state)

    print(f"\n=== {args.source}: {to_load} to load, {skipped} unchanged (skip_load), "
          f"{done_before} already converted, {len(dropped)} superseded ===")


if __name__ == "__main__":
    main()
