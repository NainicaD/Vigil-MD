"""Vigil MD — Data Acquisition DAG

Handles the full data pull from FDA and GUDID into GCS.
Runs daily, skips if nothing changed (fetch_sources.py handles this).

Tasks:
  1. check_manifest    — check if FDA data has updated
  2. fetch_all_sources — run fetch_sources.py, stream to GCS
  3. validate_gcs      — confirm expected files landed in GCS
  4. log_summary       — log file counts and sizes to Airflow
  5. alert_on_failure  — triggered only if any task fails

Teammate's DAG (vigil_md_pipeline.py) picks up from GCS
and handles preprocessing, entity resolution, and BigQuery loading.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.providers.standard.operators.python import PythonOperator, BranchPythonOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.task.trigger_rule import TriggerRule

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REPO       = Path("/Users/nainicadasari/Vigil-MD")
PYTHON     = sys.executable
BUCKET     = "vigil-md-raw-nd"
START_YEAR = 2016
END_YEAR   = 2026

default_args = {
    "owner":            "nainica",
    "retries":          2,
    "retry_delay":      timedelta(minutes=5),
    "email_on_failure": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def run_script(script: str, *args: str) -> str:
    """Run a repo script as a subprocess. Returns stdout. Raises on failure."""
    cmd = [PYTHON, str(REPO / script)] + list(args)
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, cwd=str(REPO),
        capture_output=True, text=True
    )
    if result.stdout:
        log.info(result.stdout)
    if result.stderr:
        log.warning(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"{script} failed (exit {result.returncode})\n{result.stderr}"
        )
    return result.stdout


# ── Task 1 — Check manifest ───────────────────────────────────────────────────

def check_manifest(**context):
    """
    Hit openFDA download.json and compare export_dates to last run.
    Returns 'fetch_all_sources' if anything changed, 'no_update' if not.
    """
    import requests
    from google.cloud import storage

    manifest = requests.get(
        "https://api.fda.gov/download.json", timeout=30
    ).json()
    device = manifest["results"]["device"]

    # Load saved state from GCS
    client = storage.Client()
    bucket = client.bucket(BUCKET)
    state_blob = bucket.blob("manifests/manifest_state.json")
    state = (
        json.loads(state_blob.download_as_text())
        if state_blob.exists() else {}
    )

    changed = False
    for endpoint in ["enforcement", "recall", "event"]:
        new_date = device[endpoint]["export_date"]
        old_date = state.get(f"openfda_{endpoint}_export_date")
        if new_date != old_date:
            log.info("%s changed: %s → %s", endpoint, old_date, new_date)
            changed = True
        else:
            log.info("%s unchanged: %s", endpoint, new_date)

    # Also check GUDID via RSS
    import xml.etree.ElementTree as ET
    import re
    rss = requests.get(
        "https://accessgudid.nlm.nih.gov/download.rss?files=full",
        timeout=30
    ).content
    root = ET.fromstring(rss)
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        if link and "gudid" in link.lower() and link.endswith(".zip"):
            m = re.search(r"(\d{8})", link)
            snapshot = m.group(1) if m else ""
            if snapshot != state.get("gudid_snapshot"):
                log.info("GUDID changed: %s → %s",
                         state.get("gudid_snapshot"), snapshot)
                changed = True
            else:
                log.info("GUDID unchanged: %s", snapshot)
            break

    if changed:
        log.info("Changes detected — triggering full fetch")
        return "fetch_all_sources"
    else:
        log.info("No changes — skipping fetch")
        return "no_update"


# ── Task 2 — Fetch all sources ────────────────────────────────────────────────

def fetch_all_sources(**context):
    """
    Run fetch_sources.py — streams all sources directly to GCS.
    Skips files already present. Handles retries internally.
    """
    run_script(
        "scripts/acquisition/fetch_sources.py",
        "--bucket",     BUCKET,
        "--start-year", str(START_YEAR),
        "--end-year",   str(END_YEAR),
    )
    log.info("fetch_sources.py completed successfully")


# ── Task 3 — Validate GCS ────────────────────────────────────────────────────

def validate_gcs(**context):
    """
    Test that expected files landed in GCS correctly.
    Checks:
      - enforcement zip exists
      - recall zip exists
      - at least one MAUDE quarterly file per year in range
      - GUDID zip exists
    Raises on any failure so Airflow marks the task red.
    """
    from google.cloud import storage
    client  = storage.Client()
    bucket  = client.bucket(BUCKET)
    errors  = []
    passed  = 0

    # Load manifest to get export_date
    state_blob = bucket.blob("manifests/manifest_state.json")
    state = (
        json.loads(state_blob.download_as_text())
        if state_blob.exists() else {}
    )
    export_date = state.get("openfda_enforcement_export_date", "*")

    # ── Test 1: enforcement zip exists ───────────────────────────────────────
    enf_prefix = f"raw/enforcement/{export_date}/"
    enf_blobs  = list(client.list_blobs(BUCKET, prefix=enf_prefix))
    if enf_blobs:
        log.info("✓ Enforcement: %d file(s) found", len(enf_blobs))
        passed += 1
    else:
        errors.append(f"✗ Enforcement: no files found under {enf_prefix}")

    # ── Test 2: recall zip exists ────────────────────────────────────────────
    rec_prefix = f"raw/recall/{export_date}/"
    rec_blobs  = list(client.list_blobs(BUCKET, prefix=rec_prefix))
    if rec_blobs:
        log.info("✓ Recall: %d file(s) found", len(rec_blobs))
        passed += 1
    else:
        errors.append(f"✗ Recall: no files found under {rec_prefix}")

    # ── Test 3: MAUDE has files for each year in range ───────────────────────
    evt_export = state.get("openfda_event_export_date", export_date)
    for year in range(START_YEAR, END_YEAR + 1):
        year_prefix = f"raw/event/{evt_export}/{year}"
        year_blobs  = list(client.list_blobs(BUCKET, prefix=year_prefix))
        if year_blobs:
            log.info("✓ MAUDE %d: %d file(s) found", year, len(year_blobs))
            passed += 1
        else:
            if year == END_YEAR:
                log.warning("⚠ MAUDE %d: no files (may be partial year)", year)
            else:
                errors.append(
                    f"✗ MAUDE {year}: no files found under {year_prefix}"
                )

    # ── Test 4: GUDID zip exists ─────────────────────────────────────────────
    gudid_snapshot = state.get("gudid_snapshot", "")
    gudid_prefix   = f"raw/gudid/{gudid_snapshot}/"
    gudid_blobs    = list(client.list_blobs(BUCKET, prefix=gudid_prefix))
    if gudid_blobs:
        log.info("✓ GUDID: %d file(s) found (snapshot %s)",
                 len(gudid_blobs), gudid_snapshot)
        passed += 1
    else:
        errors.append(
            f"✗ GUDID: no files found under {gudid_prefix}"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + len(errors)
    log.info("Validation: %d/%d checks passed", passed, total)

    if errors:
        for e in errors:
            log.error(e)
        raise ValueError(
            f"GCS validation failed: {len(errors)} error(s)\n"
            + "\n".join(errors)
        )

    log.info("All GCS validation checks passed ✓")


# ── Task 4 — Log summary ──────────────────────────────────────────────────────

def log_summary(**context):
    """Log total file count, total size, and per-source breakdown to Airflow."""
    from google.cloud import storage
    client = storage.Client()
    bucket = client.bucket(BUCKET)

    sources = {
        "enforcement": "raw/enforcement/",
        "recall":      "raw/recall/",
        "maude_event": "raw/event/",
        "gudid":       "raw/gudid/",
    }

    total_files = 0
    total_bytes = 0

    log.info("=" * 50)
    log.info("GCS BUCKET SUMMARY: gs://%s/raw/", BUCKET)
    log.info("=" * 50)

    for source, prefix in sources.items():
        blobs = list(client.list_blobs(BUCKET, prefix=prefix))
        zips  = [b for b in blobs if b.name.endswith(".zip")]
        size  = sum(b.size for b in zips)
        total_files += len(zips)
        total_bytes += size
        log.info(
            "%-15s %3d zip files   %6.1f MB",
            source, len(zips), size / 1e6
        )

    log.info("-" * 50)
    log.info(
        "%-15s %3d zip files   %6.1f MB",
        "TOTAL", total_files, total_bytes / 1e6
    )
    log.info("=" * 50)


# ── Task 5 — Alert on failure ─────────────────────────────────────────────────

def alert_on_failure(**context):
    """
    Called when any upstream task fails.
    Logs the failure details. Extend this to send Slack/email alerts.
    """
    failed_task = context.get("task_instance").task_id
    dag_run     = context.get("dag_run")
    log.error(
        "PIPELINE FAILURE: task '%s' failed in DAG run %s",
        failed_task, dag_run.run_id if dag_run else "unknown"
    )
    log.error(
        "Check logs at: http://localhost:8080/dags/vigil_md_acquisition/grid"
    )
    # TODO: add Slack webhook or email here when ready


# ── DAG ───────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="vigil_md_acquisition",
    description="Vigil MD — Pull all FDA sources and GUDID into GCS",
    default_args=default_args,
    start_date=datetime(2026, 10, 1),
    schedule="0 6 * * *",   # daily at 6am
    catchup=False,
    tags=["vigil-md", "acquisition", "fda"],
) as dag:

    # ── Check for updates ─────────────────────────────────────────────────────
    check = BranchPythonOperator(
        task_id="check_manifest",
        python_callable=check_manifest,
    )

    no_update = EmptyOperator(task_id="no_update")

    # ── Fetch ─────────────────────────────────────────────────────────────────
    fetch = PythonOperator(
        task_id="fetch_all_sources",
        python_callable=fetch_all_sources,
    )

    # ── Validate ──────────────────────────────────────────────────────────────
    validate = PythonOperator(
        task_id="validate_gcs",
        python_callable=validate_gcs,
    )

    # ── Log summary ───────────────────────────────────────────────────────────
    summary = PythonOperator(
        task_id="log_summary",
        python_callable=log_summary,
    )

    # ── Alert on failure ──────────────────────────────────────────────────────
    alert = PythonOperator(
        task_id="alert_on_failure",
        python_callable=alert_on_failure,
        trigger_rule=TriggerRule.ONE_FAILED,
    )

    # ── Dependencies ──────────────────────────────────────────────────────────
    check >> [fetch, no_update]
    fetch >> validate >> summary
    [fetch, validate, summary] >> alert