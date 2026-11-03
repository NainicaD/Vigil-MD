"""Tests for scripts/acquisition/gudid_convert.py: download once, keep the zip,
convert XML → NDJSON locally.

GCS is the in-memory FakeBucket from conftest.py (it models the real
blob.open() exit behaviour). HTTP is FakeSession. No GCP, no network.
"""
import hashlib
import gzip
import json

import pytest

from scripts.acquisition import gudid_convert as g
from tests.conftest import FakeBlob, FakeBucket

RAW = "raw/gudid/20261001/gudid_full_release_20261001.zip"
ND = "ndjson/gudid/20261001/full_release.ndjson.gz"


@pytest.fixture
def zip_file(tmp_path, make_gudid_zip, gudid_device):
    """A 2-part, 3-device GUDID zip on disk. Returns (path, sha256)."""
    def _make(name="g.zip", date="2026-10-01", **kw):
        data = make_gudid_zip([[gudid_device("K1"), gudid_device("K2")], [gudid_device("K3")]],
                              date=date, **kw)
        path = tmp_path / name
        path.write_bytes(data)
        return str(path), hashlib.sha256(data).hexdigest()
    return _make


def test_ndjson_name_uses_snapshot_and_fixed_key():
    assert g.ndjson_name_for("20261001") == ND


# ── process_local_zip ────────────────────────────────────────────────────────

def test_new_release_loads(fake_bucket, zip_file):
    path, sha = zip_file()
    r = g.process_local_zip(fake_bucket, path, sha, RAW, ND, None)
    assert r["action"] == "load" and r["row_count"] == 3 and r["skip_load"] is False
    assert r["ndjson_path"] == ND and r["zip_sha256"] == sha
    keys = [json.loads(l)["payload"]["publicDeviceRecordKey"]
            for l in gzip.decompress(fake_bucket.store[ND]).decode().splitlines()]
    assert keys == ["K1", "K2", "K3"]
    assert fake_bucket.store[RAW] == open(path, "rb").read()          # original zip kept
    assert fake_bucket.meta[ND] == {"zip_sha256": sha, "content_sha256": r["content_sha256"],
                                    "row_count": "3"}


def test_same_zip_bytes_copies_server_side_and_does_not_convert(fake_bucket, zip_file):
    path, sha = zip_file()
    old = "raw/gudid/20260901/gudid_full_release_20260901.zip"
    fake_bucket.store[old] = open(path, "rb").read()
    r = g.process_local_zip(fake_bucket, path, sha, RAW, ND,
                            {"zip_sha256": sha, "gcs_path": old})
    assert r == {"action": "unchanged_hash", "zip_sha256": sha}
    assert fake_bucket.store[RAW] == fake_bucket.store[old]
    assert ND not in fake_bucket.store


def test_new_bytes_same_records_removes_ndjson(fake_bucket, zip_file):
    p1, s1 = zip_file("a.zip", date="2026-10-01")
    content = g.process_local_zip(FakeBucket(), p1, s1, RAW, ND, None)["content_sha256"]
    p2, s2 = zip_file("b.zip", date="2026-11-01")       # new part names → new zip bytes
    assert s1 != s2
    r = g.process_local_zip(fake_bucket, p2, s2, RAW, ND,
                            {"zip_sha256": s1, "content_sha256": content})
    assert r["action"] == "unchanged_content" and r["row_count"] == 3
    assert ND not in fake_bucket.store and RAW in fake_bucket.store


def test_conversion_error_raises_and_leaves_no_ndjson(fake_bucket, zip_file):
    # regression: docs/issue.md #12 ("wt" mode finalized a partial NDJSON)
    path, sha = zip_file(db_total=99)
    with pytest.raises(RuntimeError, match="numberRecordsDatabase says 99"):
        g.process_local_zip(fake_bucket, path, sha, RAW, ND, None)
    assert ND not in fake_bucket.store
    assert RAW in fake_bucket.store            # the zip is uploaded first, by design


def test_ndjson_is_written_in_binary_mode(fake_bucket, zip_file, monkeypatch):
    modes = []
    real_open = FakeBlob.open
    monkeypatch.setattr(FakeBlob, "open",
                        lambda self, mode="rb", **kw: modes.append((self.name, mode)) or real_open(self, mode))
    path, sha = zip_file()
    g.convert_local_zip(fake_bucket, path, ND, sha)
    assert (ND, "wb") in modes and not any(m == "wt" for _, m in modes)


def test_put_zip_skips_existing_object(fake_bucket, zip_file):
    path, _ = zip_file()
    fake_bucket.store[RAW] = b"already there"
    assert g.put_zip(fake_bucket, path, RAW) == "exists"
    assert fake_bucket.store[RAW] == b"already there"


# ── fetch_and_convert ────────────────────────────────────────────────────────

def test_fetch_and_convert_downloads_once_and_cleans_temp(fake_bucket, fake_session,
                                                          make_gudid_zip, gudid_device, tmp_path):
    data = make_gudid_zip([[gudid_device("K1")]])
    session = fake_session({"gudid_full_release": data})
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    r = g.fetch_and_convert(session, fake_bucket, "https://x/gudid_full_release_20261001.zip",
                            RAW, ND, None, tmp_dir=str(tmp_dir))
    assert r["action"] == "load" and r["zip_sha256"] == hashlib.sha256(data).hexdigest()
    assert session.calls == ["https://x/gudid_full_release_20261001.zip"]
    assert list(tmp_dir.iterdir()) == []                 # temp file removed
