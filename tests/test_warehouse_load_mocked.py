"""Mocked-GCP tests for load_interim_to_bq.py: GCS upload and BigQuery load calls.

Input-file and CLI checks live in test_warehouse_load.py.
No credentials, bucket, or BigQuery project are needed (see fake_gcp in conftest.py).
"""
import sys
from unittest.mock import MagicMock

import pytest

from scripts.acquisition import load_interim_to_bq as mod

PROJECT = "proj"
BUCKET = "bkt"
N_TABLES = len(mod.SOURCES) + 1  # source tables + links table


@pytest.fixture
def interim(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "INTERIM", tmp_path)
    for source in mod.SOURCES:
        (tmp_path / f"{source}_flat.parquet").write_bytes(b"PAR1")
    (tmp_path / "links_maude_gudid.parquet").write_bytes(b"PAR1")
    (tmp_path / "linking_report.json").write_text("{}")
    return tmp_path


@pytest.fixture
def run(monkeypatch):
    def _run():
        monkeypatch.setattr(
            sys, "argv", ["prog", "--project", PROJECT, "--bucket", BUCKET])
        mod.main()
    return _run


def gcs_bucket(fake_gcp):
    return fake_gcp.storage.Client.return_value.bucket.return_value


def bq_client(fake_gcp):
    return fake_gcp.bigquery.Client.return_value


def uploaded_blob_names(fake_gcp):
    return [c.args[0] for c in gcs_bucket(fake_gcp).blob.call_args_list]


# ── 5. happy path ────────────────────────────────────────────────────────────

def test_all_input_files_present_pipeline_succeeds(interim, fake_gcp, run, capsys):
    run()
    out = capsys.readouterr().out
    assert "All processed tables loaded into BigQuery" in out
    for s in mod.SOURCES:
        assert f"{s}_flat" in out
    assert "links_maude_gudid" in out


# ── GCS upload ───────────────────────────────────────────────────────────────

def test_correct_gcs_bucket_selected(interim, fake_gcp, run):
    run()
    fake_gcp.storage.Client.return_value.bucket.assert_called_once_with(BUCKET)


def test_correct_blob_names(interim, fake_gcp, run):
    run()
    expected = [f"interim/{s}_flat.parquet" for s in mod.SOURCES] + [
        "interim/links_maude_gudid.parquet",
        "interim/linking_report.json",
    ]
    assert uploaded_blob_names(fake_gcp) == expected


def test_correct_local_paths_uploaded(interim, fake_gcp, run):
    run()
    blob = gcs_bucket(fake_gcp).blob.return_value
    uploaded = [c.args[0] for c in blob.upload_from_filename.call_args_list]
    expected = [str(interim / f"{s}_flat.parquet") for s in mod.SOURCES] + [
        str(interim / "links_maude_gudid.parquet"),
        str(interim / "linking_report.json"),
    ]
    assert uploaded == expected


def test_linking_report_is_optional(interim, fake_gcp, run):
    (interim / "linking_report.json").unlink()
    run()
    assert "interim/linking_report.json" not in uploaded_blob_names(fake_gcp)
    assert len(uploaded_blob_names(fake_gcp)) == N_TABLES


def test_gcs_upload_failure_propagates_and_skips_bigquery(interim, fake_gcp, run):
    blob = gcs_bucket(fake_gcp).blob.return_value
    blob.upload_from_filename.side_effect = PermissionError("403 forbidden")
    with pytest.raises(PermissionError):
        run()
    bq_client(fake_gcp).load_table_from_uri.assert_not_called()
    bq_client(fake_gcp).create_dataset.assert_not_called()


# ── BigQuery load ────────────────────────────────────────────────────────────

def test_expected_load_jobs_submitted(interim, fake_gcp, run):
    run()
    client = bq_client(fake_gcp)
    assert client.load_table_from_uri.call_count == N_TABLES
    assert client.load_table_from_uri.return_value.result.call_count == N_TABLES


def test_correct_bigquery_project_and_destination_tables(interim, fake_gcp, run):
    run()
    bq = fake_gcp.bigquery
    bq.Client.assert_called_once_with(project=PROJECT)

    base = f"{PROJECT}.{mod.DATASET}"
    bq.Dataset.assert_called_once_with(base)
    assert bq.Dataset.return_value.location == "US"
    assert bq_client(fake_gcp).create_dataset.call_args.kwargs == {"exists_ok": True}

    calls = bq_client(fake_gcp).load_table_from_uri.call_args_list
    loaded = {(c.args[0], c.args[1]) for c in calls}
    expected = {
        (f"gs://{BUCKET}/interim/{s}_flat.parquet", f"{base}.{s}_flat")
        for s in mod.SOURCES
    }
    expected.add((f"gs://{BUCKET}/interim/links_maude_gudid.parquet",
                  f"{base}.links_maude_gudid"))
    assert loaded == expected


def test_load_config_uses_parquet_and_truncate(interim, fake_gcp, run):
    run()
    configs = fake_gcp.bigquery.LoadJobConfig.call_args_list
    assert len(configs) == N_TABLES
    for c in configs:
        assert c.kwargs == {
            "source_format": "PARQUET",
            "write_disposition": "WRITE_TRUNCATE",
        }


def test_each_load_job_receives_its_config(interim, fake_gcp, run):
    run()
    for c in bq_client(fake_gcp).load_table_from_uri.call_args_list:
        assert c.kwargs["job_config"] is fake_gcp.bigquery.LoadJobConfig.return_value


def test_bigquery_job_failure_propagates(interim, fake_gcp, run):
    job = bq_client(fake_gcp).load_table_from_uri.return_value
    job.result.side_effect = RuntimeError("schema mismatch")
    with pytest.raises(RuntimeError, match="schema mismatch"):
        run()


def test_zero_row_source_table_fails_loudly(interim, fake_gcp, run):
    bq_client(fake_gcp).get_table.return_value.num_rows = 0
    with pytest.raises(RuntimeError, match="0 rows"):
        run()


def test_zero_row_links_table_fails_loudly(interim, fake_gcp, run):
    def get_table(table_id):
        empty = table_id.endswith("links_maude_gudid")
        return MagicMock(num_rows=0 if empty else 5, schema=["a"])

    bq_client(fake_gcp).get_table.side_effect = get_table
    with pytest.raises(RuntimeError, match="links_maude_gudid"):
        run()


# ── client initialisation failures ───────────────────────────────────────────

def test_storage_client_init_failure_stops_pipeline(interim, fake_gcp, run):
    fake_gcp.storage.Client.side_effect = RuntimeError("no credentials")
    with pytest.raises(RuntimeError, match="no credentials"):
        run()
    fake_gcp.bigquery.Client.assert_not_called()


def test_bigquery_client_init_failure_stops_before_loading(interim, fake_gcp, run):
    fake_gcp.bigquery.Client.side_effect = RuntimeError("bad project")
    with pytest.raises(RuntimeError, match="bad project"):
        run()
    assert fake_gcp.bigquery.Client.return_value.load_table_from_uri.call_count == 0
