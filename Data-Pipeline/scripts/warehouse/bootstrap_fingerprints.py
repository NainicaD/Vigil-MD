"""Create the MAUDE report fingerprints from the files PROD already holds (one-off).

Changed-reports-only loads (report_delta.py) skip a report whose fingerprint is known. Without
fingerprints, the next two re-exports load full files. This script writes them now, from the
files the partition state points to (manifests/partitions/maude.json, gcs_path).

Safety: a file's fingerprints are written only if
  - its records hash to the content_sha256 the state recorded when the file was loaded
    (same function as the unzip step: convert.convert_zip), so the fingerprints describe exactly
    the data that was loaded, and
  - vigil_md_ops.load_log has a row with that content_sha256 (the load committed).
A file that fails either check gets no shard: the next re-export loads it in full (as before).
Shards go straight to fingerprints/maude/current/ (the content is already in PROD). A file that
already has a current shard is skipped, so a rerun resumes.

Usage:
    python scripts/warehouse/bootstrap_fingerprints.py --project vigil-md-1792696816 \
        --bucket vigil-md-raw-nd --print-only
    python scripts/warehouse/bootstrap_fingerprints.py ... --limit 2      # check only, no writes
    python scripts/warehouse/bootstrap_fingerprints.py ... --write        # writes the shards
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import common, report_delta, schemas  # noqa: E402
from scripts.warehouse.convert import convert_zip  # noqa: E402
from scripts.warehouse.unzip_to_ndjson import download_to_temp  # noqa: E402

log = get_logger("bootstrap_fingerprints")
SOURCE = "maude"
EXIT_MISMATCH = 4


class NullWriter:
    def write(self, text: str) -> int:
        return len(text)


def render_load_log_sql(load_log: str) -> str:
    return (f"SELECT DISTINCT content_sha256 FROM `{load_log}`\n"
            "WHERE source = @source AND content_sha256 IN UNNEST(@shas)")


def fingerprint_file(bucket, entry: dict) -> tuple[dict, int, str, str]:
    """Download one zip; return (fingerprints, records, content_sha256, zip_sha256)."""
    delta = report_delta.ReportDelta(SOURCE, {})
    with tempfile.TemporaryFile() as tmp:
        zip_sha = download_to_temp(bucket, entry["gcs_path"], tmp)
        rows, content_sha = convert_zip(tmp, NullWriter(), SOURCE, delta.keep)
    return delta.fps, rows, content_sha, zip_sha


def check_file(state_entry: dict, content_sha: str, loaded_shas: set[str]) -> str | None:
    """None if the shard may be written, else the reason it may not. Pure function."""
    if content_sha != state_entry.get("content_sha256"):
        return (f"content_sha256 {content_sha[:12]}… differs from the state "
                f"{str(state_entry.get('content_sha256'))[:12]}…")
    if content_sha not in loaded_shas:
        return "no load_log row with this content_sha256"
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--limit", type=int, default=None, help="only the first N files")
    p.add_argument("--write", action="store_true", help="write the shards (default: check only)")
    p.add_argument("--print-only", action="store_true", help="print the plan, no GCP call")
    args = p.parse_args(argv)
    load_log = schemas.load_log_table(args.project)

    if args.print_only:
        print(f"input:  gs://{args.bucket}/manifests/partitions/{SOURCE}.json (gcs_path of each file)")
        print(f"check:  content_sha256 == state, and present in {load_log}:\n"
              + render_load_log_sql(load_log))
        print(f"output: gs://{args.bucket}/{report_delta.current_path(SOURCE, '<partition_key>')}"
              + ("" if args.write else "  (not written without --write)"))
        return 0

    from google.cloud import bigquery, storage
    bucket = storage.Client(project=args.project).bucket(args.bucket)
    state = common.load_partition_state(bucket, SOURCE) or {}
    keys = sorted(state)[: args.limit] if args.limit else sorted(state)
    shas = sorted({state[k]["content_sha256"] for k in keys if state[k].get("content_sha256")})
    job = bigquery.Client(project=args.project).query(
        render_load_log_sql(load_log), job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("source", "STRING", SOURCE),
                              bigquery.ArrayQueryParameter("shas", "STRING", shas)],
            maximum_bytes_billed=100 * 1024 * 1024))
    loaded = {r.content_sha256 for r in job.result()}
    log.info("%d files in the state, %d checked now; %d of their content hashes are in load_log",
             len(state), len(keys), len(loaded))

    written = skipped = failed = reports = 0
    t0 = time.time()
    for i, key in enumerate(keys, 1):
        if args.write and bucket.blob(report_delta.current_path(SOURCE, key)).exists():
            skipped += 1
            continue
        fps, rows, content_sha, _ = fingerprint_file(bucket, state[key])
        problem = check_file(state[key], content_sha, loaded)
        if problem:
            failed += 1
            log.error("  %s: NOT written: %s", key, problem)
            continue
        if len(fps) != rows:
            log.warning("  %s: %d records but %d distinct report keys", key, rows, len(fps))
        reports += len(fps)
        if args.write:
            bucket.blob(report_delta.current_path(SOURCE, key)).upload_from_string(
                report_delta.encode_shard(fps), content_type="application/gzip")
            written += 1
        log.info("  [%d/%d] %s: %d reports, content hash matches%s (%.0f min)", i, len(keys),
                 key, len(fps), ", shard written" if args.write else "", (time.time() - t0) / 60)

    print(f"\n=== {SOURCE}: {len(keys)} files, {reports} reports fingerprinted, {written} shards "
          f"written, {skipped} already had one, {failed} failed checks ===")
    if failed:
        return EXIT_MISMATCH
    return 0


if __name__ == "__main__":
    sys.exit(main())
