"""Full data pull — streams all sources directly to GCS.

Sources:
  - openFDA device/enforcement  (all records, bulk download)
  - openFDA device/recall       (all records, bulk download)
  - openFDA device/event MAUDE  (quarterly, 2016-2026, includes narratives)
  - GUDID full release          (latest monthly release)

All files stream directly to GCS — nothing saved to local disk.
A manifest state file tracks what has been pulled so re-runs skip
already-downloaded files.

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)
log = logging.getLogger("fetch_sources")

MANIFEST_URL  = "https://api.fda.gov/download.json"
GUDID_RSS_URL = "https://accessgudid.nlm.nih.gov/download.rss?files=full"
CHUNK_SIZE    = 8 * 1024 * 1024   # 8 MB per GCS write chunk
STATE_BLOB    = "manifests/manifest_state.json"


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
                           dry_run: bool) -> list[dict]:
    """Download all partitions for one openFDA endpoint."""
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
                dry_run: bool) -> dict:
    url, snapshot = latest_gudid_url(session)
    filename  = url.split("/")[-1]
    blob_name = f"raw/gudid/{snapshot}/{filename}"

    if state.get("gudid_snapshot") == snapshot:
        log.info("GUDID: snapshot %s unchanged", snapshot)
    else:
        log.info("GUDID: new snapshot %s", snapshot)

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
    args = parser.parse_args()

    from google.cloud import storage
    gcs_client = storage.Client()
    bucket = gcs_client.bucket(args.bucket)

    session = make_session()
    state   = load_state(bucket)
    all_entries: list[dict] = []

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
        )
        all_entries.extend(evt_entries)

# ── 5. GUDID ──────────────────────────────────────────────────────────────────
    gudid_entry = None
    if args.source in ("all", "gudid"):
        log.info("── GUDID ──")
        gudid_entry = fetch_gudid(session, bucket, state, args.dry_run)
        all_entries.append(gudid_entry)

    # ── 6. Save manifest ──────────────────────────────────────────────────────
    run_time   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_blob = f"manifests/pull_{run_time}.json"
    if not args.dry_run:
        bucket.blob(manifest_blob).upload_from_string(
            json.dumps(all_entries, indent=2),
            content_type="application/json",
        )
        log.info("Pull manifest → gs://%s/%s", args.bucket, manifest_blob)

    # ── 7. Update state ───────────────────────────────────────────────────────
    new_state = dict(state)  # start from existing state
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
                    if e.get("sha256") in ("already_present", "dry_run"))
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