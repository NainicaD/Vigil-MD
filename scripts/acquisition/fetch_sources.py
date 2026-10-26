"""Full data pull — streams all sources directly to GCS.

Sources:
  - openFDA device/enforcement  (all records, bulk download)
  - openFDA device/recall       (all records, bulk download)
  - openFDA device/event MAUDE  (quarterly, 2016-2026, includes narratives)
  - GUDID full release          (latest monthly release)

All files stream directly to GCS — nothing saved to local disk.
A manifest state file tracks what has been pulled so re-runs skip
already-downloaded files.

Modes (--mode):
  incremental (default) — per-partition state in manifests/partitions/.
      Unchanged fingerprint → server-side copy, no download.
      Changed → download to _incoming/, compare SHA-256. Same hash → keep
      the file, no load. New hash → add to manifests/pending/ for the
      warehouse load. Raises if no partition state exists yet.
      GUDID: a new snapshot downloads once to a local temp file, the zip is
      uploaded and the XML is converted to NDJSON locally
      (scripts/acquisition/gudid_convert.py), so nothing is read back.
  full — re-pull every partition; skip only paths that already exist.

Usage:
    python scripts/acquisition/fetch_sources.py \
        --bucket vigil-md-raw-nd \
        --start-year 2016 \
        --end-year 2026

    # Dry run — shows what would be downloaded without downloading:
    python scripts/acquisition/fetch_sources.py \
        --bucket vigil-md-raw-nd \
        --start-year 2016 \
        --end-year 2026 \
        --dry-run

    # Full re-pull (first load / backfill):
    python scripts/acquisition/fetch_sources.py \
        --bucket vigil-md-raw-nd --mode full
"""
from __future__ import annotations

import argparse
from email import parser
import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import sys as _sys

_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
log = get_logger("fetch_sources")

MANIFEST_URL  = "https://api.fda.gov/download.json"
GUDID_RSS_URL = "https://accessgudid.nlm.nih.gov/download.rss?files=full"
CHUNK_SIZE    = 8 * 1024 * 1024   # 8 MB per GCS write chunk
STATE_BLOB    = "manifests/manifest_state.json"

# Incremental mode (docs/WAREHOUSE_PLAN.md, Step 2)
PARTITIONS_PREFIX = "manifests/partitions"   # {source}.json, one file per source
PENDING_PREFIX    = "manifests/pending"      # {source}/{run_ts}.json
INCOMING_PREFIX   = "_incoming"              # temporary, before the hash compare
GUDID_PARTITION   = "full_release"           # fixed key, so each snapshot compares to the last
OPENFDA_FINGERPRINT = ("records", "size_mb")
GUDID_FINGERPRINT   = ("export_date",)


# ── HTTP session ──────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=6,
        backoff_factor=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s


# ── GCS helpers ───────────────────────────────────────────────────────────────

def gcs_exists(bucket, blob_name: str) -> bool:
    return bucket.blob(blob_name).exists()


def stream_to_gcs(session: requests.Session,
                  url: str,
                  bucket,
                  blob_name: str,
                  dry_run: bool = False) -> str:
    """
    Stream a URL directly to GCS without saving locally.
    Returns SHA-256 of the downloaded content.
    Skips if the blob already exists in GCS.
    """
    if gcs_exists(bucket, blob_name):
        log.info("  SKIP (already in GCS): %s", blob_name)
        return "already_present"

    if dry_run:
        log.info("  DRY RUN: would download %s → gs://%s/%s",
                 url, bucket.name, blob_name)
        return "dry_run"

    log.info("  Downloading → gs://%s/%s", bucket.name, blob_name)
    sha = hashlib.sha256()
    blob = bucket.blob(blob_name)

    with session.get(url, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        with blob.open("wb") as gcs_fh:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                gcs_fh.write(chunk)
                sha.update(chunk)

    checksum = sha.hexdigest()
    log.info("  Done. SHA-256: %s", checksum[:16] + "…")
    return checksum


# ── Manifest state ────────────────────────────────────────────────────────────

def load_state(bucket) -> dict:
    blob = bucket.blob(STATE_BLOB)
    if blob.exists():
        return json.loads(blob.download_as_text())
    return {}


def save_state(bucket, state: dict) -> None:
    bucket.blob(STATE_BLOB).upload_from_string(
        json.dumps(state, indent=2),
        content_type="application/json",
    )
    log.info("Manifest state saved → gs://%s/%s", bucket.name, STATE_BLOB)


# ── Partition state (incremental mode) ────────────────────────────────────────

def load_partition_state(bucket, source: str) -> dict | None:
    blob = bucket.blob(f"{PARTITIONS_PREFIX}/{source}.json")
    if blob.exists():
        return json.loads(blob.download_as_text())
    return None


def save_partition_state(bucket, source: str, part_state: dict) -> None:
    name = f"{PARTITIONS_PREFIX}/{source}.json"
    bucket.blob(name).upload_from_string(
        json.dumps(part_state, indent=2, sort_keys=True),
        content_type="application/json",
    )
    log.info("Partition state saved → gs://%s/%s", bucket.name, name)


def save_pending(bucket, source: str, run_ts: str, entries: list[dict]) -> None:
    """Write the files that must load. Writes nothing when the list is empty."""
    if not entries:
        log.info("%s: nothing pending", source)
        return
    name = f"{PENDING_PREFIX}/{source}/{run_ts}.json"
    bucket.blob(name).upload_from_string(
        json.dumps(entries, indent=2),
        content_type="application/json",
    )
    log.info("%s: %d pending file(s) → gs://%s/%s",
             source, len(entries), bucket.name, name)


def require_partition_state(bucket, source: str) -> dict:
    part_state = load_partition_state(bucket, source)
    if part_state is None:
        raise RuntimeError(
            f"No partition state for {source}. Run vigil_md_full_load first.")
    return part_state


def classify_partition(fingerprint: dict, state_entry: dict | None,
                       keys: tuple = OPENFDA_FINGERPRINT) -> str:
    """Return "new", "changed" or "same_fingerprint". Pure function.

    A null value on either side means "unknown", so the result is "changed"
    and the caller downloads the file and compares hashes.
    """
    if not state_entry:
        return "new"
    old = [state_entry.get(k) for k in keys]
    new = [fingerprint.get(k) for k in keys]
    if None in old or None in new:
        return "changed"
    return "same_fingerprint" if old == new else "changed"


def openfda_partition_key(url: str) -> str:
    """e.g. .../device/event/2024q1/device-event-0001-of-0004.json.zip
    → "2024q1/device-event-0001-of-0004.json.zip". Pure function."""
    parts = url.split("/")
    return f"{parts[-2]}/{parts[-1]}"


def fetch_partition_incremental(session: requests.Session,
                                bucket,
                                url: str,
                                blob_name: str,
                                fingerprint: dict,
                                state_entry: dict | None,
                                keys: tuple,
                                run_ts: str,
                                dry_run: bool) -> tuple[str, str]:
    """Fetch one partition in incremental mode.

    Returns (action, sha256). action is one of:
      already_present  target blob exists, fingerprint unchanged
      copied           fingerprint unchanged, server-side copy from the old path
      unchanged_hash   downloaded, but content hash equals the saved hash
      load             new or changed content, must load into BigQuery
      dry_run
    """
    kind = classify_partition(fingerprint, state_entry, keys)

    if kind == "same_fingerprint":
        if gcs_exists(bucket, blob_name):
            log.info("  SKIP (unchanged, already in GCS): %s", blob_name)
            return "already_present", state_entry.get("zip_sha256") or ""
        if dry_run:
            log.info("  DRY RUN: would copy %s → %s",
                     state_entry["gcs_path"], blob_name)
            return "dry_run", ""
        bucket.copy_blob(bucket.blob(state_entry["gcs_path"]), bucket, blob_name)
        log.info("  COPY (unchanged): %s → %s", state_entry["gcs_path"], blob_name)
        return "copied", state_entry.get("zip_sha256") or ""

    if dry_run:
        log.info("  DRY RUN: would download (%s) %s → gs://%s/%s",
                 kind, url, bucket.name, blob_name)
        return "dry_run", ""

    incoming = f"{INCOMING_PREFIX}/{run_ts}/{blob_name}"
    checksum = stream_to_gcs(session, url, bucket, incoming, dry_run)
    bucket.rename_blob(bucket.blob(incoming), blob_name)

    if state_entry and checksum == state_entry.get("zip_sha256"):
        log.info("  UNCHANGED (same SHA-256, new path): %s", blob_name)
        return "unchanged_hash", checksum
    log.info("  LOAD (%s): %s", kind, blob_name)
    return "load", checksum


def record_incremental_action(action: str, sha: str, source: str, key: str,
                              blob_name: str, export_date: str,
                              fingerprint: dict, part_state: dict,
                              pending: list, extra: dict | None = None) -> None:
    """Apply the result of one partition fetch to the state and pending list.

    Content unchanged (copied / unchanged_hash / unchanged_content): refresh
    gcs_path and the fingerprint. unchanged_content (GUDID: new zip bytes,
    same records) also refreshes zip_sha256, like the unzip step does.
    Never write content_sha256 here, and never write hashes for a file that
    must load: only load_raw_to_bq writes those, after its commit.

    extra: fields added to the pending entry (GUDID converted during the
    fetch adds content_sha256, row_count, ndjson_path, skip_load).
    """
    if action in ("copied", "unchanged_hash", "unchanged_content"):
        entry = part_state.setdefault(key, {})
        entry["gcs_path"] = blob_name
        entry["export_date"] = export_date
        entry.update(fingerprint)
        if action == "unchanged_content":
            entry["zip_sha256"] = sha
    elif action == "load":
        pending.append({
            "source":        source,
            "partition_key": key,
            "gcs_path":      blob_name,
            "export_date":   export_date,
            "records":       fingerprint.get("records"),
            "size_mb":       fingerprint.get("size_mb"),
            "zip_sha256":    sha,
            **(extra or {}),
        })


# ── Year filter ───────────────────────────────────────────────────────────────

def partition_year(partition: dict) -> int | None:
    m = re.match(r"(\d{4})", partition.get("display_name", ""))
    return int(m.group(1)) if m else None


def in_year_range(partition: dict, start: int, end: int) -> bool:
    year = partition_year(partition)
    return year is not None and start <= year <= end


# ── openFDA bulk download ─────────────────────────────────────────────────────

def fetch_openfda_endpoint(session: requests.Session,
                           bucket,
                           endpoint: str,
                           partitions: list,
                           export_date: str,
                           start_year: int,
                           end_year: int,
                           state: dict,
                           dry_run: bool,
                           mode: str = "full",
                           source: str = "",
                           part_state: dict | None = None,
                           pending: list | None = None,
                           run_ts: str = "") -> list[dict]:
    """Download all partitions for one openFDA endpoint.

    mode="full":        today's behaviour (skip only if the exact path exists).
    mode="incremental": compare fingerprint and SHA-256 per partition. Adds
                        files that must load to `pending`. Updates `part_state`
                        only for files whose content did not change.
    """
    results = []

    # Check if export_date changed since last run
    state_key = f"openfda_{endpoint}_export_date"
    if state.get(state_key) == export_date:
        log.info("%s: export_date unchanged (%s) — checking individual files",
                 endpoint, export_date)
    else:
        log.info("%s: new export_date %s (was %s)",
                 endpoint, export_date, state.get(state_key, "never"))

    # Filter partitions by year for MAUDE; keep all for enforcement/recall
    if endpoint == "event":
        selected = [p for p in partitions
                    if in_year_range(p, start_year, end_year)]
        log.info("%s: %d of %d quarterly partitions in %d–%d",
                 endpoint, len(selected), len(partitions), start_year, end_year)
    else:
        selected = partitions
        log.info("%s: %d partitions (full dataset)", endpoint, len(selected))

    for part in selected:
        url      = part["file"]
        filename = url.split("/")[-1]
        # Preserve the quarter folder in the GCS path e.g. event/2016q1/
        quarter  = url.split("/")[-2]
        blob_name = f"raw/{endpoint}/{export_date}/{quarter}/{filename}"

        if mode == "incremental":
            key = openfda_partition_key(url)
            fingerprint = {"records": part.get("records"),
                           "size_mb": part.get("size_mb")}
            action, sha = fetch_partition_incremental(
                session, bucket, url, blob_name, fingerprint,
                part_state.get(key), OPENFDA_FINGERPRINT, run_ts, dry_run)
            record_incremental_action(
                action, sha, source, key, blob_name, export_date,
                fingerprint, part_state, pending)
            checksum = action if action != "load" else sha
        else:
            checksum = stream_to_gcs(session, url, bucket, blob_name, dry_run)
        results.append({
            "source":      f"openfda_{endpoint}",
            "export_date": export_date,
            "partition":   part.get("display_name"),
            "records":     part.get("records"),
            "size_mb":     part.get("size_mb"),
            "url":         url,
            "gcs_path":    blob_name,
            "sha256":      checksum,
            "pulled_at":   datetime.now(timezone.utc).isoformat(),
        })
        time.sleep(0.3)   # polite pause between files

    return results


# ── GUDID ─────────────────────────────────────────────────────────────────────

def latest_gudid_url(session: requests.Session) -> tuple[str, str]:
    """Return (url, snapshot_date) of the latest GUDID full release."""
    root = ET.fromstring(session.get(GUDID_RSS_URL, timeout=60).content)
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if link and "gudid" in link.lower() and link.endswith(".zip"):
            m = re.search(r"(\d{8})", link)
            snapshot = m.group(1) if m else \
                datetime.now(timezone.utc).strftime("%Y%m%d")
            log.info("GUDID release: %s (snapshot %s)", link, snapshot)
            return link, snapshot
    raise RuntimeError("No GUDID full release found in RSS feed")


def fetch_gudid(session: requests.Session,
                bucket,
                state: dict,
                dry_run: bool,
                mode: str = "full",
                part_state: dict | None = None,
                pending: list | None = None,
                run_ts: str = "") -> dict:
    url, snapshot = latest_gudid_url(session)
    filename  = url.split("/")[-1]
    blob_name = f"raw/gudid/{snapshot}/{filename}"

    if state.get("gudid_snapshot") == snapshot:
        log.info("GUDID: snapshot %s unchanged", snapshot)
    else:
        log.info("GUDID: new snapshot %s", snapshot)

    if mode == "incremental":
        # The file name carries the snapshot date, so a fixed partition key
        # lets each new snapshot compare its hash against the last one.
        fingerprint = {"export_date": snapshot}
        state_entry = part_state.get(GUDID_PARTITION)
        kind = classify_partition(fingerprint, state_entry, GUDID_FINGERPRINT)
        extra = None
        if kind == "same_fingerprint" or dry_run:
            # Same snapshot: exists / server-side copy, no download
            action, sha = fetch_partition_incremental(
                session, bucket, url, blob_name, fingerprint,
                state_entry, GUDID_FINGERPRINT, run_ts, dry_run)
        else:
            # New or changed snapshot: download once to a local temp file,
            # upload the zip, convert XML → NDJSON locally (issue 11: no
            # GCS read-back in the unzip step).
            from scripts.acquisition.gudid_convert import (
                fetch_and_convert, ndjson_name_for)
            extra = fetch_and_convert(session, bucket, url, blob_name,
                                      ndjson_name_for(snapshot), state_entry)
            action = extra.pop("action")
            sha = extra["zip_sha256"]
        record_incremental_action(
            action, sha, "gudid", GUDID_PARTITION, blob_name, snapshot,
            fingerprint, part_state, pending, extra)
        checksum = action if action != "load" else sha
    else:
        checksum = stream_to_gcs(session, url, bucket, blob_name, dry_run)
    return {
        "source":   "gudid",
        "snapshot": snapshot,
        "url":      url,
        "gcs_path": blob_name,
        "sha256":   checksum,
        "pulled_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stream all FDA sources directly to GCS.")
    parser.add_argument("--bucket",     required=True,
                    help="GCS bucket name, e.g. vigil-md-raw-nd")
    parser.add_argument("--source",     required=False, default="all",
                    choices=["all", "enforcement", "recall", "maude", "gudid"],
                    help="Which source to fetch. Default: all")
    parser.add_argument("--start-year", type=int, default=2016)
    parser.add_argument("--end-year",   type=int, default=2026)
    parser.add_argument("--dry-run",    action="store_true",
                    help="Show what would be downloaded without downloading")
    parser.add_argument("--mode",       default="incremental",
                    choices=["incremental", "full"],
                    help="incremental: per-partition fingerprint + SHA-256 "
                         "checks, writes pending manifests. full: re-pull "
                         "every partition (skip only exact existing paths). "
                         "Default: incremental")
    args = parser.parse_args()

    from google.cloud import storage
    gcs_client = storage.Client()
    bucket = gcs_client.bucket(args.bucket)

    session = make_session()
    state   = load_state(bucket)
    all_entries: list[dict] = []
    run_time = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # Incremental mode: load partition state for every selected source
    # before any download, so a missing state fails fast.
    selected = ([args.source] if args.source != "all"
                else ["enforcement", "recall", "maude", "gudid"])
    part_states: dict[str, dict] = {}
    pending: dict[str, list] = {s: [] for s in selected}
    if args.mode == "incremental":
        for s in selected:
            part_states[s] = require_partition_state(bucket, s)

    def incr_kwargs(source: str) -> dict:
        if args.mode != "incremental":
            return {"mode": "full"}
        return {"mode": "incremental", "part_state": part_states[source],
                "pending": pending[source], "run_ts": run_time}

    # ── 1. openFDA manifest ───────────────────────────────────────────────────
    log.info("Fetching openFDA download manifest …")
    manifest = session.get(MANIFEST_URL, timeout=60).json()
    device   = manifest["results"]["device"]

    # ── 2. Enforcement ────────────────────────────────────────────────────────────
    enf_info = device["enforcement"]
    enf_entries = []
    if args.source in ("all", "enforcement"):
        log.info("── Enforcement ──")
        enf_entries = fetch_openfda_endpoint(
            session, bucket, "enforcement",
            enf_info["partitions"], enf_info["export_date"],
            args.start_year, args.end_year, state, args.dry_run,
            source="enforcement", **incr_kwargs("enforcement"),
        )
        all_entries.extend(enf_entries)

# ── 3. Recall ─────────────────────────────────────────────────────────────────
    rec_info = device["recall"]
    rec_entries = []
    if args.source in ("all", "recall"):
        log.info("── Recall ──")
        rec_entries = fetch_openfda_endpoint(
            session, bucket, "recall",
            rec_info["partitions"], rec_info["export_date"],
            args.start_year, args.end_year, state, args.dry_run,
            source="recall", **incr_kwargs("recall"),
        )
        all_entries.extend(rec_entries)

# ── 4. MAUDE (event) ──────────────────────────────────────────────────────────
    evt_info = device["event"]
    evt_entries = []
    if args.source in ("all", "maude"):
        log.info("── MAUDE (device/event) %d–%d ──",
             args.start_year, args.end_year)
        evt_entries = fetch_openfda_endpoint(
        session, bucket, "event",
        evt_info["partitions"], evt_info["export_date"],
        args.start_year, args.end_year, state, args.dry_run,
        source="maude", **incr_kwargs("maude"),
        )
        all_entries.extend(evt_entries)

# ── 5. GUDID ──────────────────────────────────────────────────────────────────
    gudid_entry = None
    if args.source in ("all", "gudid"):
        log.info("── GUDID ──")
        gudid_entry = fetch_gudid(session, bucket, state, args.dry_run,
                                  **incr_kwargs("gudid"))
        all_entries.append(gudid_entry)

    # ── 5b. Pending manifests + partition state (incremental only) ────────────
    # Written before manifest_state.json: if this run stops here, the next
    # check_manifest still sees the export_date change and fetches again.
    if args.mode == "incremental" and not args.dry_run:
        for s in selected:
            save_pending(bucket, s, run_time, pending[s])
            save_partition_state(bucket, s, part_states[s])

    # ── 6. Save manifest ──────────────────────────────────────────────────────
    manifest_blob = f"manifests/pull_{run_time}.json"
    if not args.dry_run:
        bucket.blob(manifest_blob).upload_from_string(
            json.dumps(all_entries, indent=2),
            content_type="application/json",
        )
        log.info("Pull manifest → gs://%s/%s", args.bucket, manifest_blob)

    # ── 7. Update state ───────────────────────────────────────────────────────
    # Re-read current state from GCS right before writing
# to avoid race condition when 4 parallel tasks all write
    try:
        current_blob = bucket.blob(STATE_BLOB)
        current_state = json.loads(current_blob.download_as_text())
    except Exception:
        current_state = dict(state)
    new_state = current_state
    
    if args.source in ("all", "enforcement"):
        new_state["openfda_enforcement_export_date"] = enf_info["export_date"]
    if args.source in ("all", "recall"):
        new_state["openfda_recall_export_date"] = rec_info["export_date"]
    if args.source in ("all", "maude"):
        new_state["openfda_event_export_date"] = evt_info["export_date"]
    if args.source in ("all", "gudid") and gudid_entry:
        new_state["gudid_snapshot"] = gudid_entry["snapshot"]
    new_state["last_pull"] = run_time
    new_state["start_year"] = args.start_year
    new_state["end_year"] = args.end_year   
    if not args.dry_run:
        save_state(bucket, new_state)

    # ── 8. Summary ────────────────────────────────────────────────────────────
    skipped   = sum(1 for e in all_entries
                    if e.get("sha256") in ("already_present", "dry_run", "copied",
                                           "unchanged_hash", "unchanged_content"))
    downloaded = len(all_entries) - skipped

    print("\n" + "="*60)
    print("FETCH SUMMARY")
    print("="*60)
    print(f"Total files:   {len(all_entries)}")
    print(f"Downloaded:    {downloaded}")
    print(f"Skipped:       {skipped} (already in GCS)")
    print(f"Dry run:       {args.dry_run}")
    print(f"\nGCS bucket:    gs://{args.bucket}/")
    print(f"State file:    gs://{args.bucket}/{STATE_BLOB}")

    by_source: dict[str, int] = {}
    for e in all_entries:
        src = e["source"]
        by_source[src] = by_source.get(src, 0) + 1
    print("\nFiles by source:")
    for src, count in by_source.items():
        print(f"  {src}: {count}")


if __name__ == "__main__":
    main()