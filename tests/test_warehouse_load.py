
"""Tests for interim Parquet -> GCS -> BigQuery loading."""

import sys

import pytest

from scripts.acquisition import load_interim_to_bq as mod

PROJECT = "proj"
BUCKET = "bkt"


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
            sys, "argv",
            ["prog", "--project", PROJECT, "--bucket", BUCKET],
        )
        mod.main()

    return _run


def uploaded_blob_names(fake_gcp):
    bucket = fake_gcp.storage.Client.return_value.bucket.return_value
    return [call.args[0] for call in bucket.blob.call_args_list]


@pytest.mark.parametrize("argv", [
    ["prog"],
    ["prog", "--project", "p"],
    ["prog", "--bucket", "b"],
])
def test_missing_required_args_exit(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 2


@pytest.mark.parametrize("filename", [
    "maude_flat.parquet",
    "links_maude_gudid.parquet",
    "recall_flat.parquet",
    "gudid_flat.parquet",
])
def test_missing_required_file_fails_before_upload(
    interim, fake_gcp, run, filename
):
    path = interim / filename
    if not path.exists():
        pytest.skip(f"{filename} is not part of this fixture")

    path.unlink()

    with pytest.raises(FileNotFoundError, match=filename.split(".")[0]):
        run()

    assert uploaded_blob_names(fake_gcp) == []


def test_all_missing_files_are_reported_together(interim, fake_gcp, run):
    missing = ["recall_flat.parquet", "gudid_flat.parquet"]

    for filename in missing:
        (interim / filename).unlink()

    with pytest.raises(FileNotFoundError) as exc:
        run()

    message = str(exc.value)
    assert all(name.removesuffix(".parquet") in message for name in missing)
    assert uploaded_blob_names(fake_gcp) == []
