"""Tests for scripts/warehouse/bootstrap_fingerprints.py. FakeBucket + fake BigQuery. No network."""
import io
import types

import pytest

from scripts.warehouse import bootstrap_fingerprints as B
from scripts.warehouse import common, report_delta as rd
from scripts.warehouse.convert import convert_zip
from tests.conftest import FakeBucket

K1, K2 = "2026q1/device-event-0001-of-0002.json.zip", "2026q1/device-event-0002-of-0002.json.zip"


def recs(*keys):
    return [{"mdr_report_key": k, "device": [{"udi_di": "1"}]} for k in keys]


@pytest.fixture
def env(make_openfda_zip, monkeypatch):
    bucket = FakeBucket()
    state = {}
    for key, records in ((K1, recs("1", "2")), (K2, recs("3"))):
        path = f"raw/event/2026-11-19/{key}"
        data = make_openfda_zip(records)
        bucket.store[path] = data
        _, sha = convert_zip(io.BytesIO(data), B.NullWriter(), "maude")
        state[key] = {"gcs_path": path, "content_sha256": sha}
    monkeypatch.setattr(common, "load_partition_state", lambda b, s: state)
    loaded = {"shas": {v["content_sha256"] for v in state.values()}}

    class BQ:
        def query(self, sql, job_config=None):
            rows = [types.SimpleNamespace(content_sha256=s) for s in loaded["shas"]]
            return types.SimpleNamespace(result=lambda: rows)
    from google.cloud import bigquery, storage
    monkeypatch.setattr(bigquery, "Client", lambda project: BQ())
    monkeypatch.setattr(storage, "Client", lambda project: types.SimpleNamespace(bucket=lambda n: bucket))
    run = lambda *a: B.main(["--project", "p", "--bucket", "bkt", *a])
    return bucket, state, loaded, run


def test_check_only_writes_nothing(env):
    bucket, _, _, run = env
    assert run() == 0
    assert not any(n.startswith(rd.PREFIX) for n in bucket.store)


def test_write_creates_one_current_shard_per_file(env):
    bucket, _, _, run = env
    assert run("--write") == 0
    assert set(rd.decode_shard(bucket.store[rd.current_path("maude", K1)])) == {"1", "2"}
    assert set(rd.decode_shard(bucket.store[rd.current_path("maude", K2)])) == {"3"}
    assert rd.load_lookup(bucket, "maude", "2026q1").keys() == {"1", "2", "3"}


def test_a_content_mismatch_gets_no_shard(env):
    bucket, state, _, run = env
    state[K2]["content_sha256"] = "something-else"
    assert run("--write") == B.EXIT_MISMATCH
    assert rd.current_path("maude", K1) in bucket.store
    assert rd.current_path("maude", K2) not in bucket.store


def test_a_file_missing_from_load_log_gets_no_shard(env):
    bucket, state, loaded, run = env
    loaded["shas"].discard(state[K1]["content_sha256"])
    assert run("--write") == B.EXIT_MISMATCH
    assert rd.current_path("maude", K1) not in bucket.store


def test_a_rerun_skips_files_that_have_a_shard(env, monkeypatch):
    bucket, _, _, run = env
    run("--write")
    calls = []
    monkeypatch.setattr(B, "fingerprint_file", lambda *a: calls.append(a))
    assert run("--write") == 0 and calls == []


def test_fingerprints_match_what_the_unzip_step_computes(env):
    """The same report gives the same fingerprint here and in unzip (report_delta.ReportDelta)."""
    bucket, state, _, _ = env
    fps, rows, sha, _ = B.fingerprint_file(bucket, state[K1])
    d = rd.ReportDelta("maude", {})
    for r in recs("1", "2"):
        d.keep(r)
    assert fps == d.fps and rows == 2 and sha == state[K1]["content_sha256"]
