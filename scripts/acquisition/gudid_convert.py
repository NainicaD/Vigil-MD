"""GUDID: download once, keep the zip, convert XML → NDJSON from the local copy.

Why here and not in the unzip step: the worker runs outside Google Cloud.
Converting later would read the full release back out of GCS (egress).
This module downloads the zip from AccessGUDID to a local temp file once:

  1. upload the original zip to raw/gudid/{snapshot}/   (source of record)
  2. convert the XML from the local file, stream NDJSON to
     ndjson/gudid/{snapshot}/full_release.ndjson        (no GCS read-back)

Hash checks (same rules as the other sources):
  zip SHA-256 == saved zip_sha256        → "unchanged_hash"    no convert, no load
  content SHA-256 == saved content_sha256 → "unchanged_content" NDJSON deleted, no load
  otherwise                               → "load"             pending entry is complete

The caller (fetch_sources.fetch_gudid) owns the partition state and the
pending manifest. Nothing here writes state.
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse.convert import NDJSON_PREFIX, Utf8Writer, convert_zip
log = get_logger("gudid_convert")

CHUNK_SIZE      = 8 * 1024 * 1024   # same as fetch_sources.py
GUDID_PARTITION = "full_release"    # same key as fetch_sources.GUDID_PARTITION


def ndjson_name_for(snapshot: str) -> str:
    return f"{NDJSON_PREFIX}/gudid/{snapshot}/{GUDID_PARTITION}.ndjson"


# ── Steps ─────────────────────────────────────────────────────────────────────

def download_to_file(session, url: str, fh) -> str:
    """Stream a URL into a local file. Returns SHA-256 of the bytes."""
    sha = hashlib.sha256()
    with session.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            fh.write(chunk)
            sha.update(chunk)
    fh.flush()
    return sha.hexdigest()


def put_zip(bucket, local_path: str, blob_name: str,
            copy_from: str | None = None) -> str:
    """Make sure the original zip is at blob_name. Returns exists|copied|uploaded.

    copy_from: an existing GCS object with the same bytes. A server-side copy
    sends no data from this machine.
    """
    if bucket.blob(blob_name).exists():
        return "exists"
    if copy_from and copy_from != blob_name and bucket.blob(copy_from).exists():
        bucket.copy_blob(bucket.blob(copy_from), bucket, blob_name)
        log.info("  COPY zip: %s → %s", copy_from, blob_name)
        return "copied"
    bucket.blob(blob_name).upload_from_filename(local_path)
    log.info("  UPLOAD zip → gs://%s/%s", bucket.name, blob_name)
    return "uploaded"


def convert_local_zip(bucket, local_path: str, ndjson_name: str,
                      zip_sha: str) -> tuple[int, str]:
    """Convert a local GUDID zip and stream the NDJSON to GCS.

    Binary BlobWriter + Utf8Writer: on any error BlobWriter.terminate() runs,
    so no partial NDJSON is left behind ("wt" mode would finalize it). Hashes
    go into the blob metadata, so a later full load can reuse this file
    without converting again.
    """
    blob = bucket.blob(ndjson_name)
    with open(local_path, "rb") as zf, blob.open("wb") as raw:
        rows, content_sha = convert_zip(zf, Utf8Writer(raw), "gudid")
    blob.metadata = {"zip_sha256": zip_sha, "content_sha256": content_sha,
                     "row_count": str(rows)}
    blob.patch()
    log.info("  NDJSON: %d devices → gs://%s/%s", rows, bucket.name, ndjson_name)
    return rows, content_sha


def process_local_zip(bucket, local_path: str, zip_sha: str, blob_name: str,
                      ndjson_name: str, state_entry: dict | None) -> dict:
    """Apply the hash rules to a downloaded GUDID zip. Returns a result dict."""
    if state_entry and zip_sha == state_entry.get("zip_sha256"):
        put_zip(bucket, local_path, blob_name, copy_from=state_entry.get("gcs_path"))
        log.info("  UNCHANGED (same zip SHA-256): %s", blob_name)
        return {"action": "unchanged_hash", "zip_sha256": zip_sha}

    put_zip(bucket, local_path, blob_name)
    rows, content_sha = convert_local_zip(bucket, local_path, ndjson_name, zip_sha)

    if state_entry and content_sha == state_entry.get("content_sha256"):
        bucket.blob(ndjson_name).delete()
        log.info("  UNCHANGED (same content SHA-256, %d devices): NDJSON removed", rows)
        return {"action": "unchanged_content", "zip_sha256": zip_sha,
                "content_sha256": content_sha, "row_count": rows}

    return {"action": "load", "zip_sha256": zip_sha, "content_sha256": content_sha,
            "row_count": rows, "ndjson_path": ndjson_name, "skip_load": False}


def fetch_and_convert(session, bucket, url: str, blob_name: str, ndjson_name: str,
                      state_entry: dict | None, tmp_dir: str | None = None) -> dict:
    """Download the GUDID zip once to a temp file, then process_local_zip."""
    with tempfile.NamedTemporaryFile(suffix=".zip", dir=tmp_dir) as tmp:
        log.info("  Downloading GUDID → local temp file %s", tmp.name)
        zip_sha = download_to_file(session, url, tmp)
        log.info("  Done. SHA-256: %s", zip_sha[:16] + "…")
        return process_local_zip(bucket, tmp.name, zip_sha, blob_name,
                                 ndjson_name, state_entry)
