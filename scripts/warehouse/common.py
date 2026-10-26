"""Shared helpers for the warehouse scripts: sources, GCS paths, pending files.

The pending manifest (manifests/pending/{source}/*.json) is the hand-off
between steps:
  fetch_sources.py   writes entries for new or changed files (--scope changed)
  unzip_to_ndjson.py adds content_sha256 / row_count / ndjson_path / skip_load,
                     and writes a "full_*" manifest itself for --scope all
  load_raw_to_bq.py  loads the entries, then deletes the manifest
"""
from __future__ import annotations

import json
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.acquisition.fetch_sources import (  # noqa: E402  (reuse, do not copy)
    GUDID_PARTITION,
    PENDING_PREFIX,
    load_partition_state,
    openfda_partition_key,
    save_partition_state,
)
from scripts.warehouse.convert import NDJSON_PREFIX, NDJSON_SUFFIX  # noqa: E402
from scripts.warehouse.schemas import DATASETS, SOURCES  # noqa: E402,F401

# Warehouse source name → GCS folder under raw/
ENDPOINTS = {
    "enforcement": "enforcement",
    "recall":      "recall",
    "maude":       "event",
    "gudid":       "gudid",
}

__all__ = [
    "DATASETS", "ENDPOINTS", "GUDID_PARTITION", "NDJSON_PREFIX", "PENDING_PREFIX",
    "SOURCES",
    "load_partition_state", "save_partition_state", "ndjson_path",
    "pick_latest", "list_all_latest", "read_pending", "write_pending",
]


# ── Paths ─────────────────────────────────────────────────────────────────────

def ndjson_path(source: str, ingest_date: str, partition_key: str) -> str:
    return f"{NDJSON_PREFIX}/{source}/{ingest_date}/{partition_key}{NDJSON_SUFFIX}"


# ── --scope all: newest folder only ───────────────────────────────────────────

def pick_latest(blob_names: list[str], source: str) -> list[dict]:
    """Return one entry per partition in the newest export folder. Pure function.

    Only the single newest export_date (or GUDID snapshot) folder for the
    endpoint is used. Older folders hold older versions of the same
    partitions; reading them would load the same records many times.
    """
    endpoint = ENDPOINTS[source]
    prefix = f"raw/{endpoint}/"
    zips = [n for n in blob_names if n.startswith(prefix) and n.endswith(".zip")]
    if not zips:
        return []

    newest = max(n.split("/")[2] for n in zips)
    entries = []
    for name in sorted(zips):
        parts = name.split("/")
        if parts[2] != newest:
            continue
        if source == "gudid":
            key = GUDID_PARTITION            # raw/gudid/{snapshot}/{file}.zip
        else:
            key = openfda_partition_key(name)  # raw/{ep}/{date}/{quarter}/{file}
        entries.append({
            "source":        source,
            "partition_key": key,
            "gcs_path":      name,
            "export_date":   newest,
            "records":       None,
            "size_mb":       None,
            "zip_sha256":    None,
        })

    if source == "gudid" and len(entries) != 1:
        raise RuntimeError(
            f"Expected 1 GUDID zip in raw/gudid/{newest}/, found {len(entries)}")
    return entries


def list_all_latest(bucket, source: str) -> list[dict]:
    names = [b.name for b in bucket.client.list_blobs(
        bucket.name, prefix=f"raw/{ENDPOINTS[source]}/")]
    return pick_latest(names, source)


# ── Pending manifests ─────────────────────────────────────────────────────────

def read_pending(bucket, source: str) -> list[tuple[str, list[dict]]]:
    """Return [(blob_name, entries), …] for every pending manifest, oldest first."""
    blobs = sorted(bucket.client.list_blobs(
        bucket.name, prefix=f"{PENDING_PREFIX}/{source}/"), key=lambda b: b.name)
    return [(b.name, json.loads(b.download_as_text()))
            for b in blobs if b.name.endswith(".json")]


def write_pending(bucket, blob_name: str, entries: list[dict]) -> None:
    bucket.blob(blob_name).upload_from_string(
        json.dumps(entries, indent=2), content_type="application/json")
