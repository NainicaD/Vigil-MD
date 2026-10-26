"""Tests for scripts/warehouse/common.py and scripts/warehouse/unzip_to_ndjson.py.

GCS is the in-memory FakeBucket; main() runs with fake_gcp, so no real storage
client is created. No network.
"""
import hashlib
import gzip
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.warehouse import common
from scripts.warehouse import unzip_to_ndjson as u
from tests.conftest import FakeBucket

REPO = str(Path(__file__).resolve().parents[1])
D = "2026-10-25"


# ── pick_latest (--scope all) ────────────────────────────────────────────────

NAMES = [
    "raw/event/2026-10-01/2024q1/device-event-0001-of-0002.json.zip",
    "raw/event/2026-10-01/2024q1/device-event-0002-of-0002.json.zip",
    "raw/event/2026-10-20/2024q1/device-event-0001-of-0002.json.zip",
    "raw/event/2026-10-20/2024q2/device-event-0001-of-0001.json.zip",
    "raw/event/2026-10-20/2024q2/notes.txt",
    "raw/enforcement/2026-10-20/enforcement/device-enforcement-0001-of-0001.json.zip",
]


def test_pick_latest_uses_only_the_newest_export_folder():
    got = common.pick_latest(NAMES, "maude")
    assert [g["gcs_path"] for g in got] == [NAMES[2], NAMES[3]]
    assert got[0]["partition_key"] == "2024q1/device-event-0001-of-0002.json.zip"
    assert all(g["export_date"] == "2026-10-20" and g["records"] is None for g in got)


def test_pick_latest_gudid_uses_the_fixed_key():
    (g,) = common.pick_latest(["raw/gudid/20261001/a_20261001.zip",
                               "raw/gudid/20261101/a_20261101.zip"], "gudid")
    assert g["partition_key"] == "full_release" and g["export_date"] == "20261101"


def test_pick_latest_two_gudid_zips_in_one_folder_raise():
    with pytest.raises(RuntimeError, match="Expected 1 GUDID zip"):
        common.pick_latest(["raw/gudid/20261101/a.zip", "raw/gudid/20261101/b.zip"], "gudid")


def test_pick_latest_empty():
    assert common.pick_latest([], "recall") == []


# ── apply_content_check ──────────────────────────────────────────────────────

@pytest.mark.parametrize("state_entry, must_load", [
    (None, True), ({"content_sha256": "old"}, True), ({"content_sha256": "h"}, False)])
def test_apply_content_check(state_entry, must_load):
    entry = {}
    assert u.apply_content_check(entry, 2, "h", "nd", state_entry) is must_load
    assert entry == {"row_count": 2, "content_sha256": "h", "ndjson_path": "nd",
                     "skip_load": not must_load}


# ── process_entry ────────────────────────────────────────────────────────────

ZIP_PATH = "raw/enforcement/2026-10-20/enforcement/device-enforcement-0001-of-0001.json.zip"
KEY = "enforcement/device-enforcement-0001-of-0001.json.zip"
RECORDS = [{"recall_number": "Z-1"}, {"recall_number": "Z-2"}]


@pytest.fixture
def bucket_with_zip(make_openfda_zip):
    bucket = FakeBucket()
    data = make_openfda_zip(RECORDS)
    bucket.store[ZIP_PATH] = data
    return bucket, hashlib.sha256(data).hexdigest()


def entry_for(**kw):
    return {"source": "enforcement", "partition_key": KEY, "gcs_path": ZIP_PATH,
            "export_date": "2026-10-20", "records": 2, "size_mb": 0.1, **kw}


def test_process_entry_writes_ndjson_and_fills_the_entry(bucket_with_zip):
    bucket, zsha = bucket_with_zip
    entry = entry_for(zip_sha256=zsha)
    assert u.process_entry(bucket, "enforcement", D, entry, {}) is True
    nd = common.ndjson_path("enforcement", D, KEY)
    assert [json.loads(l)["payload"] for l in gzip.decompress(bucket.store[nd]).decode().splitlines()] == RECORDS
    assert entry["ndjson_path"] == nd and entry["row_count"] == 2 and entry["skip_load"] is False


def test_process_entry_zip_hash_mismatch_raises(bucket_with_zip):
    bucket, _ = bucket_with_zip
    with pytest.raises(RuntimeError, match="SHA-256 differs"):
        u.process_entry(bucket, "enforcement", D, entry_for(zip_sha256="not-it"), {})


def test_process_entry_unchanged_content_refreshes_pointers_only(bucket_with_zip):
    bucket, zsha = bucket_with_zip
    first = entry_for()
    u.process_entry(bucket, "enforcement", D, first, {})
    state = {KEY: {"content_sha256": first["content_sha256"], "zip_sha256": "old",
                   "gcs_path": "old/path", "export_date": "2026-10-01"}}
    again = entry_for()
    assert u.process_entry(bucket, "enforcement", D, again, state) is False
    assert again["skip_load"] is True
    assert state[KEY] == {"content_sha256": first["content_sha256"], "zip_sha256": zsha,
                          "gcs_path": ZIP_PATH, "export_date": "2026-10-20",
                          "records": 2, "size_mb": 0.1}


# ── main() ───────────────────────────────────────────────────────────────────

@pytest.fixture
def run_main(fake_gcp, monkeypatch, bucket_with_zip):
    bucket, _ = bucket_with_zip
    fake_gcp.storage.Client.return_value.bucket.return_value = bucket

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["unzip_to_ndjson.py", "--bucket", "bkt",
                                          "--source", "enforcement", "--ingest-date", D, *args])
        u.main()
    return bucket, run


def test_main_changed_skips_entries_already_converted(run_main):
    bucket, run = run_main
    done = entry_for(partition_key="other", gcs_path="raw/other.zip", content_sha256="c",
                     row_count=9, ndjson_path="ndjson/x")         # e.g. GUDID from the fetch
    name = "manifests/pending/enforcement/20261025T060000Z.json"
    bucket.store[name] = json.dumps([done, entry_for()]).encode()
    run("--scope", "changed")
    saved = json.loads(bucket.store[name])
    assert saved[0] == done                                       # untouched
    assert saved[1]["row_count"] == 2 and "content_sha256" in saved[1]
    assert "raw/other.zip" not in bucket.store                    # never read


def test_main_all_writes_a_full_manifest(run_main):
    bucket, run = run_main
    run("--scope", "all")
    (name,) = [n for n in bucket.store if n.startswith("manifests/pending/enforcement/full_")]
    (entry,) = json.loads(bucket.store[name])
    assert entry["gcs_path"] == ZIP_PATH and entry["row_count"] == 2


def test_main_nothing_pending_writes_nothing(run_main):
    bucket, run = run_main
    before = dict(bucket.store)
    run("--scope", "changed")
    assert bucket.store == before


def test_print_only_imports_no_gcp():
    code = ("import runpy, sys; sys.argv=['u','--bucket','b','--source','maude','--scope','all',"
            "'--ingest-date','2026-10-25','--print-only'];"
            "runpy.run_path('scripts/warehouse/unzip_to_ndjson.py', run_name='__main__');"
            "print('GCP', any(m.startswith('google.cloud') for m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "GCP False" in r.stdout and "full_<run_ts>.json" in r.stdout
