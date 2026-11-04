"""Tests for the incremental mode of scripts/acquisition/fetch_sources.py.

GCS is the in-memory FakeBucket, HTTP is FakeSession (both in conftest.py).
main() runs with fake_gcp, so no real storage client is created. No network.
"""
import hashlib
import json
import sys

import pytest

from scripts.acquisition import fetch_sources as fs
from tests.conftest import FakeBucket

URL = "https://download.open.fda.gov/device/event/2024q1/device-event-0001-of-0004.json.zip"
KEY = "2024q1/device-event-0001-of-0004.json.zip"
OLD = f"raw/event/2026-10-01/{KEY}"
NEW = f"raw/event/2026-10-20/{KEY}"
DATA = b"zip-bytes-v1"
SHA = hashlib.sha256(DATA).hexdigest()
FP = {"records": 10, "size_mb": 1.5}
K = fs.OPENFDA_FINGERPRINT


def fetch_one(state_entry, fake_session, data=DATA, fingerprint=FP, pre=None, dry_run=False):
    """Run one partition through fetch_partition_incremental + record_incremental_action."""
    bucket, session = FakeBucket(), fake_session({"device-event": data})
    bucket.store.update(pre or {})
    part_state = {KEY: dict(state_entry)} if state_entry else {}
    pending = []
    action, sha = fs.fetch_partition_incremental(
        session, bucket, URL, NEW, fingerprint, part_state.get(KEY), K, "RUN", dry_run)
    fs.record_incremental_action(action, sha, "maude", KEY, NEW, "2026-10-20",
                                 fingerprint, part_state, pending)
    return action, bucket, session, part_state, pending


STATE = {"gcs_path": OLD, "records": 10, "size_mb": 1.5, "zip_sha256": SHA, "content_sha256": "C"}


# ── classify_partition / keys ────────────────────────────────────────────────

@pytest.mark.parametrize("state_entry, fingerprint, expected", [
    (None, FP, "new"),
    ({}, FP, "new"),
    ({"records": 10, "size_mb": 1.5}, FP, "same_fingerprint"),
    ({"records": 11, "size_mb": 1.5}, FP, "changed"),
    ({"records": None, "size_mb": None}, FP, "changed"),
    ({"records": 10, "size_mb": 1.5}, {"records": None, "size_mb": 1.5}, "changed"),
])
def test_classify_partition(state_entry, fingerprint, expected):
    assert fs.classify_partition(fingerprint, state_entry) == expected


def test_classify_partition_gudid_fingerprint():
    g = fs.GUDID_FINGERPRINT
    assert fs.classify_partition({"export_date": "20261001"}, {"export_date": "20261001"}, g) == "same_fingerprint"
    assert fs.classify_partition({"export_date": "20261101"}, {"export_date": "20261001"}, g) == "changed"


def test_openfda_partition_key_is_quarter_and_file():
    assert fs.openfda_partition_key(URL) == KEY


# ── one partition ────────────────────────────────────────────────────────────

def test_same_fingerprint_copies_without_download(fake_session):
    action, bucket, session, part_state, pending = fetch_one(STATE, fake_session, pre={OLD: DATA})
    assert action == "copied" and session.calls == [] and pending == []
    assert bucket.store[NEW] == DATA
    assert part_state[KEY]["gcs_path"] == NEW and part_state[KEY]["zip_sha256"] == SHA


def test_same_fingerprint_and_target_exists_is_skipped(fake_session):
    action, bucket, session, _, pending = fetch_one(STATE, fake_session, pre={OLD: DATA, NEW: DATA})
    assert action == "already_present" and session.calls == [] and pending == []


def test_changed_fingerprint_same_hash_is_not_pending(fake_session):
    st = dict(STATE, records=9)
    action, bucket, session, part_state, pending = fetch_one(st, fake_session)
    assert action == "unchanged_hash" and len(session.calls) == 1 and pending == []
    assert bucket.store[NEW] == DATA
    assert not any(n.startswith(fs.INCOMING_PREFIX + "/") for n in bucket.store)
    assert part_state[KEY]["records"] == 10                 # fingerprint refreshed


def test_new_content_is_pending_and_state_untouched(fake_session):
    st = dict(STATE, records=9)
    action, bucket, _, part_state, pending = fetch_one(st, fake_session, data=b"zip-bytes-v2")
    assert action == "load"
    assert pending == [{"source": "maude", "partition_key": KEY, "gcs_path": NEW,
                        "export_date": "2026-10-20", "records": 10, "size_mb": 1.5,
                        "zip_sha256": hashlib.sha256(b"zip-bytes-v2").hexdigest()}]
    assert part_state[KEY] == st          # hashes are written only after the raw load commits
    assert not any(n.startswith(fs.INCOMING_PREFIX + "/") for n in bucket.store)


def test_null_fingerprint_downloads_and_compares(fake_session):
    action, *_ = fetch_one(dict(STATE, records=None, size_mb=None), fake_session)
    assert action == "unchanged_hash"


def test_new_partition_loads(fake_session):
    action, _, _, part_state, pending = fetch_one(None, fake_session)
    assert action == "load" and len(pending) == 1 and part_state == {}


def test_dry_run_downloads_and_writes_nothing(fake_session):
    action, bucket, session, _, pending = fetch_one(None, fake_session, dry_run=True)
    assert action == "dry_run" and session.calls == [] and bucket.store == {} and pending == []


# ── state helpers ────────────────────────────────────────────────────────────

def test_missing_partition_state_raises(fake_bucket):
    with pytest.raises(RuntimeError, match="Run vigil_md_full_load first"):
        fs.require_partition_state(fake_bucket, "maude")


def test_empty_pending_writes_nothing(fake_bucket):
    fs.save_pending(fake_bucket, "maude", "RUN", [])
    assert fake_bucket.store == {}


def test_pending_and_partition_state_paths(fake_bucket):
    fs.save_pending(fake_bucket, "maude", "RUN", [{"a": 1}])
    fs.save_partition_state(fake_bucket, "maude", {"k": {}})
    assert json.loads(fake_bucket.store["manifests/pending/maude/RUN.json"]) == [{"a": 1}]
    assert fs.load_partition_state(fake_bucket, "maude") == {"k": {}}


# ── GUDID (converted during the fetch) ───────────────────────────────────────

RAW1 = "raw/gudid/20261001/gudid_full_release_20261001.zip"
RAW2 = "raw/gudid/20261101/gudid_full_release_20261101.zip"
ND2 = "ndjson/gudid/20261101/full_release.ndjson.gz"


@pytest.fixture
def gudid_zips(make_gudid_zip, gudid_device):
    devices = [[gudid_device("K1"), gudid_device("K2")]]
    z1, z2 = make_gudid_zip(devices, date="2026-10-01"), make_gudid_zip(devices, date="2026-11-01")
    return z1, z2, hashlib.sha256(z1).hexdigest(), hashlib.sha256(z2).hexdigest()


def run_gudid(fake_session, zip_data, state_entry, snapshot="20261101", pre=None, dry_run=False):
    link = f"https://accessgudid.nlm.nih.gov/release_files/download/gudid_full_release_{snapshot}.zip"
    session = fake_session({
        "download.rss": f"<rss><channel><item><link>{link}</link></item></channel></rss>".encode(),
        "gudid_full_release_": zip_data})
    bucket = FakeBucket()
    bucket.store.update(pre or {})
    part_state = {fs.GUDID_PARTITION: dict(state_entry)} if state_entry else {}
    pending = []
    entry = fs.fetch_gudid(session, bucket, {}, dry_run, mode="incremental",
                           part_state=part_state, pending=pending, run_ts="RUN")
    zip_calls = [c for c in session.calls if c.endswith(".zip")]
    return entry, bucket, zip_calls, part_state, pending


def test_gudid_new_snapshot_pending_entry_is_already_converted(fake_session, gudid_zips):
    _, z2, _, s2 = gudid_zips
    entry, bucket, zip_calls, part_state, pending = run_gudid(fake_session, z2, None)
    assert entry["sha256"] == s2 and len(zip_calls) == 1
    (p,) = pending
    assert p["ndjson_path"] == ND2 and p["row_count"] == 2 and p["skip_load"] is False
    assert p["zip_sha256"] == s2 and "content_sha256" in p and "action" not in p
    assert RAW2 in bucket.store and ND2 in bucket.store and part_state == {}


def test_gudid_same_snapshot_does_not_download(fake_session, gudid_zips):
    _, z2, _, s2 = gudid_zips
    st = {"gcs_path": RAW2, "export_date": "20261101", "zip_sha256": s2, "content_sha256": "c"}
    entry, _, zip_calls, _, pending = run_gudid(fake_session, z2, st, pre={RAW2: z2})
    assert entry["sha256"] == "already_present" and zip_calls == [] and pending == []


def test_gudid_new_bytes_same_records_is_unchanged_content(fake_session, gudid_zips):
    z1, z2, s1, s2 = gudid_zips
    _, _, _, _, (first,) = run_gudid(fake_session, z1, None, snapshot="20261001")
    st = {"gcs_path": RAW1, "export_date": "20261001", "zip_sha256": s1,
          "content_sha256": first["content_sha256"]}
    entry, bucket, _, part_state, pending = run_gudid(fake_session, z2, st, pre={RAW1: z1})
    assert entry["sha256"] == "unchanged_content" and pending == [] and ND2 not in bucket.store
    st2 = part_state[fs.GUDID_PARTITION]
    assert st2["zip_sha256"] == s2 and st2["gcs_path"] == RAW2 and st2["export_date"] == "20261101"
    assert st2["content_sha256"] == first["content_sha256"]


def test_gudid_identical_bytes_under_new_snapshot_is_copied(fake_session, gudid_zips):
    _, z2, _, s2 = gudid_zips
    st = {"gcs_path": RAW1, "export_date": "20261001", "zip_sha256": s2, "content_sha256": "c"}
    entry, bucket, _, _, pending = run_gudid(fake_session, z2, st, pre={RAW1: z2})
    assert entry["sha256"] == "unchanged_hash" and pending == []
    assert bucket.store[RAW2] == z2 and ND2 not in bucket.store


def test_gudid_dry_run_writes_nothing(fake_session, gudid_zips):
    _, z2, _, _ = gudid_zips
    st = {"gcs_path": RAW1, "export_date": "20261001", "zip_sha256": "x", "content_sha256": "c"}
    entry, bucket, zip_calls, _, pending = run_gudid(fake_session, z2, st, dry_run=True)
    assert entry["sha256"] == "dry_run" and zip_calls == [] and bucket.store == {} and pending == []


def test_gudid_full_mode_streams_zip_without_converting(fake_session, gudid_zips):
    _, z2, _, s2 = gudid_zips
    link = "https://accessgudid.nlm.nih.gov/release_files/download/gudid_full_release_20261101.zip"
    session = fake_session({
        "download.rss": f"<rss><channel><item><link>{link}</link></item></channel></rss>".encode(),
        "gudid_full_release_": z2})
    bucket = FakeBucket()
    entry = fs.fetch_gudid(session, bucket, {}, False)
    assert entry["sha256"] == s2 and bucket.store[RAW2] == z2 and ND2 not in bucket.store


# ── main() ───────────────────────────────────────────────────────────────────

ENF_URL = "https://download.open.fda.gov/device/enforcement/device-enforcement-0001-of-0001.json.zip"
ENF_KEY = "enforcement/device-enforcement-0001-of-0001.json.zip"
ENF_BLOB = f"raw/enforcement/2026-10-20/{ENF_KEY}"
MANIFEST = {"results": {"device": {
    "enforcement": {"export_date": "2026-10-20", "partitions": [
        {"file": ENF_URL, "display_name": "All", "records": 5, "size_mb": 0.1}]},
    "recall": {"export_date": "2026-10-20", "partitions": []},
    "event": {"export_date": "2026-10-20", "partitions": []},
}}}
OLD_STATE = {"openfda_enforcement_export_date": "2026-10-01", "openfda_recall_export_date": "R",
             "openfda_event_export_date": "M", "gudid_snapshot": "20261001",
             "last_pull": "X", "start_year": 2016, "end_year": 2026}


@pytest.fixture
def env(fake_gcp, fake_session, monkeypatch):
    bucket = FakeBucket("vigil-md-raw-nd")
    fake_gcp.storage.Client.return_value.bucket.return_value = bucket
    bucket.store[fs.STATE_BLOB] = json.dumps(OLD_STATE).encode()
    session = fake_session({"download.json": MANIFEST, "device-enforcement": b"enf-zip"})
    monkeypatch.setattr(fs, "make_session", lambda: session)
    monkeypatch.setattr(fs.time, "sleep", lambda *_: None)

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["fetch_sources.py", "--bucket", bucket.name, *args])
        fs.main()
    return bucket, session, run


def test_main_incremental_checks_all_state_before_any_download(env):
    bucket, session, run = env
    bucket.store["manifests/partitions/enforcement.json"] = b"{}"   # recall, maude, gudid missing
    with pytest.raises(RuntimeError, match="No partition state for recall"):
        run("--source", "all")
    assert session.calls == []


def test_main_incremental_new_partition_writes_pending(env):
    bucket, session, run = env
    bucket.store["manifests/partitions/enforcement.json"] = b"{}"
    run("--source", "enforcement")
    (name,) = [n for n in bucket.store if n.startswith("manifests/pending/enforcement/")]
    (entry,) = json.loads(bucket.store[name])
    assert entry["gcs_path"] == ENF_BLOB and entry["zip_sha256"] == hashlib.sha256(b"enf-zip").hexdigest()
    assert bucket.store[ENF_BLOB] == b"enf-zip"
    assert json.loads(bucket.store["manifests/partitions/enforcement.json"]) == {}


def test_main_full_mode_never_reads_partition_state(env):
    bucket, _, run = env
    run("--source", "enforcement", "--mode", "full")
    assert bucket.store[ENF_BLOB] == b"enf-zip"
    assert not any(n.startswith(("manifests/partitions/", "manifests/pending/")) for n in bucket.store)


@pytest.mark.parametrize("mode", ["full", "incremental"])
def test_main_keeps_every_manifest_state_key(env, mode):
    bucket, _, run = env
    bucket.store["manifests/partitions/enforcement.json"] = b"{}"
    run("--source", "enforcement", "--mode", mode)
    state = json.loads(bucket.store[fs.STATE_BLOB])
    assert set(OLD_STATE) <= set(state)
    assert state["openfda_enforcement_export_date"] == "2026-10-20"
    assert state["gudid_snapshot"] == "20261001"          # other sources untouched
