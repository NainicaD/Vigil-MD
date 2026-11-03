"""Tests for the shared fakes in conftest.py (no GCP, no network).

The warehouse tests trust these fakes, so the fakes must behave like the real
libraries. A fake that did not hid a real bug once (docs/issue.md #12).
"""
import io
import json
import xml.etree.ElementTree as ET
import zipfile

import pytest

NS = "{http://www.fda.gov/cdrh/gudid}"


# ── Logstash guard ───────────────────────────────────────────────────────────

def test_logstash_handler_never_connects(monkeypatch):
    from scripts import logger

    def connect(self):
        raise AssertionError("LogstashHandler tried to connect")
    monkeypatch.setattr(logger.LogstashHandler, "_connect", connect)
    log = logger.get_logger("test_fakes_logstash_guard")
    assert any(isinstance(h, logger.LogstashHandler) for h in log.handlers)
    log.info("this must not reach Logstash")


# ── FakeBucket / FakeBlob ────────────────────────────────────────────────────

def test_wb_write_is_stored_on_success(fake_bucket):
    with fake_bucket.blob("a.ndjson").open("wb") as fh:
        fh.write(b"x\n")
    assert fake_bucket.store["a.ndjson"] == b"x\n"


def test_wb_write_leaves_no_object_on_error(fake_bucket):
    with pytest.raises(RuntimeError):
        with fake_bucket.blob("a.ndjson").open("wb") as fh:
            fh.write(b"partial")
            raise RuntimeError("conversion failed")
    assert not fake_bucket.blob("a.ndjson").exists()


def test_wt_write_is_finalized_even_on_error(fake_bucket):
    # Models google-cloud-storage: TextIOWrapper.close() finalizes the upload
    with pytest.raises(RuntimeError):
        with fake_bucket.blob("a.ndjson").open("wt") as fh:
            fh.write("partial")
            raise RuntimeError("conversion failed")
    assert fake_bucket.store["a.ndjson"] == b"partial"


def test_rb_reads_and_missing_object_raises(fake_bucket):
    fake_bucket.store["a"] = b"data"
    assert fake_bucket.blob("a").open("rb").read() == b"data"
    with pytest.raises(FileNotFoundError):
        fake_bucket.blob("missing").download_as_bytes()


def test_upload_download_delete(fake_bucket, tmp_path):
    p = tmp_path / "z.zip"
    p.write_bytes(b"zip")
    fake_bucket.blob("raw/z.zip").upload_from_filename(str(p))
    fake_bucket.blob("m.json").upload_from_string('{"a": 1}', content_type="application/json")
    assert fake_bucket.blob("raw/z.zip").size == 3
    assert json.loads(fake_bucket.blob("m.json").download_as_text()) == {"a": 1}
    fake_bucket.blob("m.json").delete()
    assert not fake_bucket.blob("m.json").exists()
    with pytest.raises(FileNotFoundError):
        fake_bucket.blob("m.json").delete()


def test_copy_and_rename(fake_bucket):
    fake_bucket.store["old"] = b"bytes"
    fake_bucket.copy_blob(fake_bucket.blob("old"), fake_bucket, "copy")
    fake_bucket.rename_blob(fake_bucket.blob("old"), "new")
    assert fake_bucket.store == {"copy": b"bytes", "new": b"bytes"}


def test_metadata_patch_persists(fake_bucket):
    fake_bucket.store["n"] = b""
    blob = fake_bucket.blob("n")
    blob.metadata = {"content_sha256": "c"}
    blob.patch()
    assert fake_bucket.blob("n").metadata == {"content_sha256": "c"}


def test_list_blobs_filters_sorts_and_limits(fake_bucket):
    for n in ["manifests/pending/b.json", "manifests/pending/a.json", "raw/x.zip"]:
        fake_bucket.store[n] = b""
    names = [b.name for b in fake_bucket.client.list_blobs("bkt", prefix="manifests/pending/")]
    assert names == ["manifests/pending/a.json", "manifests/pending/b.json"]
    assert len(fake_bucket.client.list_blobs("bkt", prefix="", max_results=1)) == 1


# ── FakeSession ──────────────────────────────────────────────────────────────

def test_session_routes_records_and_streams(fake_session):
    s = fake_session({"download.json": {"results": {}}, "file.zip": b"abcdef",
                      "broken": (500, b"")})
    assert s.get("https://api.fda.gov/download.json", timeout=30).json() == {"results": {}}
    with s.get("https://x/file.zip", stream=True) as resp:
        assert b"".join(resp.iter_content(chunk_size=4)) == b"abcdef"
    with pytest.raises(RuntimeError):
        s.get("https://x/broken").raise_for_status()
    with pytest.raises(RuntimeError):
        s.get("https://x/unknown").raise_for_status()
    assert len(s.calls) == 4


# ── Zip builders ─────────────────────────────────────────────────────────────

def test_openfda_zip_has_one_json_member_with_results(make_openfda_zip):
    data = make_openfda_zip([{"a": 1}, {"a": 2}])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        (member,) = z.namelist()
        doc = json.loads(z.read(member))
    assert member.endswith(".json") and doc["results"] == [{"a": 1}, {"a": 2}] and "meta" in doc


def test_gudid_zip_parts_and_header_counts(make_gudid_zip, gudid_device):
    data = make_gudid_zip([[gudid_device("k1"), gudid_device("k2")], [gudid_device("k3")]])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = sorted(z.namelist())
        roots = {n: ET.fromstring(z.read(n)) for n in names}
    assert names == ["FULLDownload_Part1_Of_2_2026-10-01.xml",
                     "FULLDownload_Part2_Of_2_2026-10-01.xml"]
    p1 = roots[names[0]]
    assert p1.find(f"{NS}header/{NS}database/{NS}numberRecordXML").text == "2"
    assert p1.find(f"{NS}header/{NS}database/{NS}numberRecordsDatabase").text == "3"
    assert len(p1.findall(f"{NS}device")) == 2


def test_gudid_zip_header_overrides(make_gudid_zip, gudid_device):
    data = make_gudid_zip([[gudid_device("k1")]], per_part=5, db_total=9)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))
    db = root.find(f"{NS}header/{NS}database")
    assert db.find(f"{NS}numberRecordXML").text == "5"
    assert db.find(f"{NS}numberRecordsDatabase").text == "9"
