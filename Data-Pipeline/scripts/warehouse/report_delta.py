"""Load only the MAUDE reports that changed (docs/issue.md #4 and #17).

openFDA re-exports rewrite every MAUDE file even when the reports are the same: reports move
between the files of a quarter, lists are reordered, and an internal counter
(device_sequence_number) is renumbered. 2026-11-02 -> 03: 0 of 196,449 reports changed in
2016q1; 2026-11-03 -> 17: 6.6% (2026q1) to 13.2% (2016q1) changed once those are ignored. The
per-file checks (zip and content hash) see every file as new, so every report was loaded again.

The unzip step fingerprints each report and writes only new or changed reports to the NDJSON.

Fingerprint: SHA-256 (first 16 hex) of the whole record in canonical form: keys sorted, lists of
objects sorted, lists of scalars sorted and de-duplicated, IGNORE fields dropped at any depth.
Every other field counts, also fields no table reads today (FDA edits them for real).

Store (GCS, one shard per source file, so a re-export of one file replaces one shard):
  fingerprints/{source}/current/{partition_key}.json.gz        what PROD holds
  fingerprints/{source}/pending/{ingest_date}/{partition_key}.json.gz
                                                               written by unzip, promoted after load
A shard maps report key -> fingerprint for every report in that file version. A report can sit
in two shards of a quarter (it moved and the old file was not re-exported); the most recently
promoted shard wins. Promotion is a server-side copy, after the file's RAW load commits (or at
unzip for a file with nothing to load), so a failed load never marks a report as seen.

No shards yet (first run, new quarter, new FP_VERSION) -> every report is "new": a full load as
before, which also writes the shards.
"""
from __future__ import annotations

import gzip
import hashlib
import json

FP_VERSION = 1
PREFIX = "fingerprints"
DELTA_SOURCES = {
    "maude": {"key": "mdr_report_key", "ignore": frozenset({"device_sequence_number"})},
}


# ── Pure ──────────────────────────────────────────────────────────────────────

def canonical(value, ignore: frozenset = frozenset()):
    """Canonical form: dict keys sorted (by json.dumps), IGNORE keys dropped at any depth,
    lists of objects sorted, lists of scalars sorted and de-duplicated."""
    if isinstance(value, dict):
        return {k: canonical(v, ignore) for k, v in value.items() if k not in ignore}
    if isinstance(value, list):
        items = [canonical(v, ignore) for v in value]
        dumped = [json.dumps(v, sort_keys=True, separators=(",", ":")) for v in items]
        if all(not isinstance(v, (dict, list)) for v in items):
            dumped = sorted(set(dumped))        # e.g. patient_problems repeated per patient
        else:
            dumped = sorted(dumped)             # e.g. two identical devices stay two devices
        return [json.loads(d) for d in dumped]
    return value


def fingerprint(record: dict, ignore: frozenset = frozenset()) -> str:
    text = json.dumps(canonical(record, ignore), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def quarter(partition_key: str) -> str:
    """'2026q1/device-event-0001-of-0009.json.zip' -> '2026q1'."""
    return partition_key.split("/", 1)[0]


def current_path(source: str, partition_key: str) -> str:
    return f"{PREFIX}/{source}/current/{partition_key}.json.gz"


def pending_path(source: str, ingest_date: str, partition_key: str) -> str:
    return f"{PREFIX}/{source}/pending/{ingest_date}/{partition_key}.json.gz"


def encode_shard(fps: dict[str, str]) -> bytes:
    body = {"fp_version": FP_VERSION, "fingerprints": fps}
    return gzip.compress(json.dumps(body, separators=(",", ":")).encode("utf-8"), mtime=0)


def decode_shard(data: bytes) -> dict[str, str] | None:
    """Shard -> {key: fp}; None for another FP_VERSION (treated as missing)."""
    body = json.loads(gzip.decompress(data))
    return body["fingerprints"] if body.get("fp_version") == FP_VERSION else None


def merge_shards(shards: list[tuple[object, dict[str, str]]]) -> dict[str, str]:
    """[(promoted_at, {key: fp})] -> {key: fp}; the most recently promoted shard wins."""
    lookup: dict[str, str] = {}
    for _, fps in sorted(shards, key=lambda s: s[0]):
        lookup.update(fps)
    return lookup


class ReportDelta:
    """keep(record) for convert_zip: True for a new or changed report. Collects the
    fingerprints of every record of the file (the new shard)."""

    def __init__(self, source: str, lookup: dict[str, str]):
        cfg = DELTA_SOURCES[source]
        self.key, self.ignore, self.lookup = cfg["key"], cfg["ignore"], lookup
        self.fps: dict[str, str] = {}
        self.records = self.written = self.new = self.changed = 0

    def keep(self, record: dict) -> bool:
        key = record.get(self.key)
        if key is None:
            raise RuntimeError(f"record without {self.key}: cannot fingerprint it")
        fp = fingerprint(record, self.ignore)
        self.records += 1
        self.fps[str(key)] = fp
        old = self.lookup.get(str(key))
        if old == fp:
            return False
        self.written += 1
        if old is None:
            self.new += 1
        else:
            self.changed += 1
        return True

    def summary(self) -> dict:
        return {"fp_version": FP_VERSION, "records": self.records, "written": self.written,
                "new": self.new, "changed": self.changed,
                "unchanged": self.records - self.written}


# ── GCS ───────────────────────────────────────────────────────────────────────

def load_lookup(bucket, source: str, qtr: str) -> dict[str, str]:
    """{key: fp} of one quarter, from the current shards (newest promotion wins)."""
    prefix = f"{PREFIX}/{source}/current/{qtr}/"
    shards = []
    for blob in bucket.client.list_blobs(bucket, prefix=prefix):
        fps = decode_shard(blob.download_as_bytes())
        if fps is not None:
            shards.append((blob.time_created, fps))
    return merge_shards(shards)


def write_pending(bucket, source: str, ingest_date: str, partition_key: str,
                  fps: dict[str, str]) -> str:
    path = pending_path(source, ingest_date, partition_key)
    bucket.blob(path).upload_from_string(encode_shard(fps), content_type="application/gzip")
    return path


def promote(bucket, source: str, ingest_date: str, partition_key: str) -> bool:
    """Pending shard -> current (server-side copy, then delete). False if there is none
    (source without fingerprints, or already promoted)."""
    pending = bucket.blob(pending_path(source, ingest_date, partition_key))
    if not pending.exists():
        return False
    bucket.copy_blob(pending, bucket, current_path(source, partition_key))
    pending.delete()
    return True
