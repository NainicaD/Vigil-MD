"""Tests that validate files landed in GCS correctly after fetch_sources.py.

Requires GCP credentials and the vigil-md-raw-nd bucket to be accessible.
Run after fetch_sources.py completes to confirm the pull was successful.

Marked `integration` and deselected by default (see pytest.ini).
Run explicitly with: pytest -m integration
The vigil_md_acquisition DAG runs this file in its validate_gcs task and sets
VIGIL_START_YEAR / VIGIL_END_YEAR to the fetched year range.
"""
import json
import os
import warnings

import pytest

try:
    from google.cloud import storage
    GCS_AVAILABLE = True
except ImportError:
    GCS_AVAILABLE = False

BUCKET = "vigil-md-raw-nd"
START_YEAR = int(os.environ.get("VIGIL_START_YEAR", "2016"))
END_YEAR = int(os.environ.get("VIGIL_END_YEAR", "2026"))
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not GCS_AVAILABLE,
        reason="google-cloud-storage not installed"
    ),
]


@pytest.fixture(scope="module")
def gcs_client():
    return storage.Client()


@pytest.fixture(scope="module")
def manifest_state(gcs_client):
    """Load the saved manifest state from GCS."""
    blob = gcs_client.bucket(BUCKET).blob("manifests/manifest_state.json")
    if blob.exists():
        return json.loads(blob.download_as_text())
    return {}


@pytest.fixture(scope="module")
def bucket(gcs_client):
    return gcs_client.bucket(BUCKET)


# ── Enforcement ───────────────────────────────────────────────────────────────

def test_enforcement_folder_exists(gcs_client, manifest_state):
    export_date = manifest_state.get("openfda_enforcement_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/enforcement/{export_date}/enforcement/"))
    assert len(blobs) > 0, "No enforcement files found in GCS"


def test_enforcement_files_are_zips(gcs_client, manifest_state):
    export_date = manifest_state.get("openfda_enforcement_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/enforcement/{export_date}/enforcement/"))
    zip_files = [b for b in blobs if b.name.endswith(".zip")]
    assert len(zip_files) > 0, "No zip files in enforcement folder"


def test_enforcement_files_not_empty(gcs_client, manifest_state):
    export_date = manifest_state.get("openfda_enforcement_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/enforcement/{export_date}/enforcement/"))
    for blob in blobs:
        assert blob.size > 0, f"Empty file: {blob.name}"


# ── Recall ────────────────────────────────────────────────────────────────────

def test_recall_folder_exists(gcs_client, manifest_state):
    export_date = manifest_state.get("openfda_recall_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/recall/{export_date}/"))
    assert len(blobs) > 0, "No recall files found in GCS"


def test_recall_files_not_empty(gcs_client, manifest_state):
    export_date = manifest_state.get("openfda_recall_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/recall/{export_date}/"))
    for blob in blobs:
        assert blob.size > 0, f"Empty file: {blob.name}"


# ── MAUDE ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("year", range(START_YEAR, END_YEAR + 1))
def test_maude_year_exists(gcs_client, manifest_state, year):
    export_date = manifest_state.get("openfda_event_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/event/{export_date}/{year}"))
    if not blobs and year == END_YEAR:
        warnings.warn(f"No MAUDE files for {year} (may be a partial year)")
        return
    assert len(blobs) > 0, f"No MAUDE files for {year}"


def test_maude_files_not_empty(gcs_client, manifest_state):
    export_date = manifest_state.get("openfda_event_export_date", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/event/{export_date}/"))
    zips = [b for b in blobs if b.name.endswith(".zip")]
    assert len(zips) >= 30, f"Expected at least 30 MAUDE files, got {len(zips)}"
    for blob in zips:
        assert blob.size > 0, f"Empty file: {blob.name}"


# ── GUDID ─────────────────────────────────────────────────────────────────────

def test_gudid_folder_exists(gcs_client, manifest_state):
    snapshot = manifest_state.get("gudid_snapshot", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/gudid/{snapshot}/"))
    assert len(blobs) > 0, "No GUDID files found in GCS"


def test_gudid_file_is_large(gcs_client, manifest_state):
    """GUDID full release should be at least 100 MB."""
    snapshot = manifest_state.get("gudid_snapshot", "")
    blobs = list(gcs_client.list_blobs(
        BUCKET, prefix=f"raw/gudid/{snapshot}/"))
    zips = [b for b in blobs if b.name.endswith(".zip")]
    assert len(zips) > 0
    total_size = sum(b.size for b in zips)
    assert total_size > 100 * 1024 * 1024, \
        f"GUDID file too small: {total_size / 1e6:.1f} MB"


# ── Manifest state ────────────────────────────────────────────────────────────

def test_manifest_state_exists(gcs_client):
    blob = gcs_client.bucket(BUCKET).blob("manifests/manifest_state.json")
    assert blob.exists(), "manifest_state.json not found in GCS"


def test_manifest_has_all_export_dates(manifest_state):
    required_keys = [
        "openfda_enforcement_export_date",
        "openfda_recall_export_date",
        "openfda_event_export_date",
        "gudid_snapshot",
    ]
    for key in required_keys:
        assert key in manifest_state, f"Missing key in manifest: {key}"
        assert manifest_state[key], f"Empty value for key: {key}"