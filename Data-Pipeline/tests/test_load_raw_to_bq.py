"""Tests for scripts/warehouse/load_raw_to_bq.py (idempotent raw load).

BigQuery is the fake_gcp MagicMock; GCS is the in-memory FakeBucket. The SQL is
checked as text (structure and order). No GCP, no network.
"""
import json
import re
import sys
import types

import pytest

from scripts.warehouse import load_raw_to_bq as L
from tests.conftest import FakeBucket

RAW, TMP, LOG = "p.vigil_md_raw.maude_raw", "p.vigil_md_ops._tmp_x", "p.vigil_md_ops.load_log"
ENTRY = {"source": "maude", "partition_key": "2024q1/a.zip", "gcs_path": "raw/event/D/2024q1/a.zip",
         "export_date": "D", "records": 5, "size_mb": 1.0, "zip_sha256": "z1",
         "content_sha256": "c1", "row_count": 5, "ndjson_path": "ndjson/maude/X/2024q1/a.zip.ndjson",
         "skip_load": False}


# ── SQL ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def sql():
    return L.render_load_sql(RAW, TMP, LOG)


def test_delete_is_limited_to_one_day_and_one_file(sql):
    delete = re.search(rf"DELETE FROM `{re.escape(RAW)}`\s+WHERE (.+?);", sql, re.S).group(1)
    assert "_ingest_date = @ingest_date" in delete and "_source_file = @source_file" in delete


def test_row_count_check_comes_before_the_transaction(sql):
    assert sql.index("RAISE") < sql.index("BEGIN TRANSACTION")


def test_statement_order(sql):
    order = ["BEGIN TRANSACTION", f"DELETE FROM `{RAW}`", f"INSERT INTO `{RAW}`",
             f"INSERT INTO `{LOG}`", "COMMIT TRANSACTION"]
    positions = [sql.index(s) for s in order]
    assert positions == sorted(positions)


def test_every_sql_parameter_is_bound(sql):
    used = set(re.findall(r"@(\w+)", sql)) | set(re.findall(r"@(\w+)", L.render_already_loaded_sql(LOG)))
    bound = {n for n, _, _ in L.query_params(ENTRY, "maude", "2026-10-25", "incremental")}
    assert used <= bound, used - bound


def test_query_params_values():
    p = {n: (t, v) for n, t, v in L.query_params(ENTRY, "maude", "2026-10-25", "full")}
    assert p["ingest_date"] == ("DATE", "2026-10-25")
    assert p["source_file"] == ("STRING", ENTRY["gcs_path"])
    assert p["expected_rows"] == ("INT64", 5) and p["mode"] == ("STRING", "full")


# ── helpers ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, scope, expected", [
    ("manifests/pending/maude/20261025T060000Z.json", "changed", True),
    ("manifests/pending/maude/full_20261025T060000Z.json", "changed", False),
    ("manifests/pending/maude/full_20261025T060000Z.json", "all", True),
    ("manifests/pending/maude/20261025T060000Z.json", "all", False),
])
def test_manifest_in_scope(name, scope, expected):
    assert L.manifest_in_scope(name, scope) is expected


@pytest.mark.parametrize("entry, match", [
    ({"gcs_path": "x"}, "Run unzip_to_ndjson.py"),
    ({"gcs_path": "x", "content_sha256": "c", "row_count": 0, "ndjson_path": "n"}, "row_count is 0"),
])
def test_check_entry_rejects_unready_entries(entry, match):
    with pytest.raises(RuntimeError, match=match):
        L.check_entry(entry)


def test_check_entry_accepts_skip_load_without_hashes():
    L.check_entry({"gcs_path": "x", "skip_load": True})


def test_tmp_table_is_always_in_ops_dataset():
    assert L.tmp_table_name("p", "maude", "T", 3) == "p.vigil_md_ops._tmp_maude_T_3"


def test_state_after_load_has_the_hashes():
    st = L.state_after_load(ENTRY, "NOW")
    assert st == {"gcs_path": ENTRY["gcs_path"], "export_date": "D", "records": 5, "size_mb": 1.0,
                  "zip_sha256": "z1", "content_sha256": "c1", "loaded_at": "NOW"}


# ── BigQuery calls (fake_gcp) ────────────────────────────────────────────────

def test_load_entry_loads_runs_script_and_drops_temp(fake_gcp):
    client = fake_gcp.bigquery.Client.return_value
    L.load_entry(client, "bkt", "p", "maude", "2026-10-25", "incremental", ENTRY, TMP)
    client.create_table.assert_called_once()
    uri, dest = client.load_table_from_uri.call_args.args[:2]
    assert uri == f"gs://bkt/{ENTRY['ndjson_path']}" and dest == TMP
    assert TMP in client.query.call_args.args[0]
    client.delete_table.assert_called_once_with(TMP, not_found_ok=True)


def test_load_entry_drops_temp_even_on_error(fake_gcp):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = RuntimeError("row count mismatch")
    with pytest.raises(RuntimeError, match="row count mismatch"):
        L.load_entry(client, "bkt", "p", "maude", "2026-10-25", "incremental", ENTRY, TMP)
    client.delete_table.assert_called_once_with(TMP, not_found_ok=True)


@pytest.mark.parametrize("n, expected", [(0, False), (1, True)])
def test_already_loaded_reads_the_count(fake_gcp, n, expected):
    client = fake_gcp.bigquery.Client.return_value
    client.query.return_value.result.return_value = iter([types.SimpleNamespace(n=n)])
    assert L.already_loaded(client, LOG, ENTRY, "maude") is expected


# ── main() orchestration ─────────────────────────────────────────────────────

DAILY = "manifests/pending/maude/20261025T060000Z.json"
FULL = "manifests/pending/maude/full_20261025T070000Z.json"
E1 = ENTRY
E2 = dict(ENTRY, partition_key="2024q2/b.zip", gcs_path="raw/event/D/2024q2/b.zip", content_sha256="c2")
E3 = dict(ENTRY, partition_key="2024q3/c.zip", gcs_path="raw/event/D/2024q3/c.zip", skip_load=True)


@pytest.fixture
def env(fake_gcp, monkeypatch):
    bucket = FakeBucket()
    fake_gcp.storage.Client.return_value.bucket.return_value = bucket
    bucket.store[DAILY] = json.dumps([E1, E2, E3]).encode()
    bucket.store[FULL] = json.dumps([dict(E1, gcs_path="FULL")]).encode()
    bucket.store["manifests/partitions/maude.json"] = json.dumps({"2024q1/a.zip": {"records": 4}}).encode()
    loads = []
    monkeypatch.setattr(L, "already_loaded", lambda client, ll, e, s: e["content_sha256"] == "c2")
    monkeypatch.setattr(L, "load_entry", lambda client, bn, p, s, d, m, e, tmp: loads.append(e["gcs_path"]))

    def run(scope="changed"):
        monkeypatch.setattr(sys, "argv", ["x", "--project", "p", "--bucket", "bkt", "--source", "maude",
                                          "--scope", scope, "--ingest-date", "2026-10-25"])
        L.main()
    return bucket, loads, run


def state_of(bucket):
    return json.loads(bucket.store["manifests/partitions/maude.json"])


def test_main_loads_new_skips_loaded_and_skip_load(env):
    bucket, loads, run = env
    run()
    assert loads == ["raw/event/D/2024q1/a.zip"]                 # only E1 loads
    st = state_of(bucket)
    assert st["2024q1/a.zip"]["content_sha256"] == "c1" and st["2024q1/a.zip"]["records"] == 5
    assert st["2024q2/b.zip"]["content_sha256"] == "c2"         # already loaded → state healed
    assert "2024q3/c.zip" not in st                              # skip_load: no state write here


def test_main_deletes_only_its_own_manifest(env):
    bucket, _, run = env
    run("changed")
    assert DAILY not in bucket.store and FULL in bucket.store


def test_main_scope_all_reads_only_full_manifests(env):
    bucket, loads, run = env
    run("all")
    assert loads == ["FULL"] and DAILY in bucket.store and FULL not in bucket.store


def test_main_load_error_keeps_the_manifest(env, monkeypatch):
    bucket, _, run = env

    def boom(*a):
        raise RuntimeError("BigQuery error")
    monkeypatch.setattr(L, "load_entry", boom)
    with pytest.raises(RuntimeError, match="BigQuery error"):
        run()
    assert DAILY in bucket.store


def test_main_nothing_pending_returns_quietly(env):
    bucket, loads, run = env
    del bucket.store[DAILY]
    run("changed")
    assert loads == [] and FULL in bucket.store
