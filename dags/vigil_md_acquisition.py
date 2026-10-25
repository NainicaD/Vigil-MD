"""Vigil MD — Data Acquisition DAG

Handles the full data pull from FDA and GUDID into GCS.
Runs daily, skips if nothing changed (fetch_sources.py handles this).

Tasks:
  1. check_manifest    — check if FDA data has updated
  2. fetch.*           — run fetch_sources.py per source, stream to GCS
  3. validate_gcs      — confirm expected files landed in GCS
  4. log_summary       — log file counts and sizes to Airflow
  5. alert_on_failure  — triggered only if any task fails

Teammate's DAG (vigil_md_pipeline.py) picks up from GCS
and handles preprocessing, entity resolution, and BigQuery loading.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.sdk import TaskGroup
from airflow.providers.standard.operators.python import PythonOperator, BranchPythonOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.task.trigger_rule import TriggerRule

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REPO       = Path(__file__).resolve().parents[1]
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


def run_pytest(test_path: str, marker: str, env: dict[str, str] | None = None) -> None:
    """Run a pytest file as a subprocess. Raises on failure, skips, or no tests.

    A skipped test means a check did not run (e.g. a missing library), so it
    counts as a failure here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.xml"
        cmd = [PYTHON, "-m", "pytest", test_path, "-m", marker,
               "-v", "-p", "no:cacheprovider", f"--junitxml={report}"]
        log.info("Running: %s", " ".join(cmd))
        result = subprocess.run(
            cmd, cwd=str(REPO),
            capture_output=True, text=True,
            env={**os.environ, **(env or {})},
        )
        if result.stdout:
            log.info(result.stdout)
        if result.stderr:
            log.warning(result.stderr)
        if result.returncode != 0:
            raise RuntimeError(
                f"pytest {test_path} failed (exit {result.returncode})\n"
                f"{result.stdout[-3000:]}"
            )
        suite = ET.parse(report).getroot()
        if suite.tag == "testsuites":
            suite = suite[0]
        tests = int(suite.get("tests", 0))
        skipped = int(suite.get("skipped", 0))
    if tests == 0 or skipped:
        raise RuntimeError(
            f"pytest {test_path}: {tests} tests, {skipped} skipped. "
            "Every check must run."
        )
    log.info("pytest %s: %d tests passed", test_path, tests)


# ── Task 1 — Check manifest ───────────────────────────────────────────────────

def check_manifest(**context):
    """
    Hit openFDA download.json and compare export_dates to last run.
    Returns the four fetch.* task IDs if anything changed, 'no_update' if not.
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
        return [f"fetch.fetch_{s}" for s in ("enforcement", "recall", "maude", "gudid")]
    else:
        log.info("No changes — skipping fetch")
        return "no_update"


    


# ── Task 2 — Fetch all sources ────────────────────────────────────────────────

def fetch_one_source(source: str, **context):
    """Run fetch_sources.py for a single source."""
    run_script(
        "scripts/acquisition/fetch_sources.py",
        "--bucket",     BUCKET,
        "--source",     source,
        "--start-year", str(START_YEAR),
        "--end-year",   str(END_YEAR),
    )
    log.info("%s fetch completed successfully", source)


# ── Task 3 — Validate GCS ────────────────────────────────────────────────────

def validate_gcs(**context):
    """
    Test that expected files landed in GCS correctly.
    Runs tests/test_raw_data_integration.py (the single source for these
    checks): enforcement, recall, MAUDE per year in range, GUDID, manifest.
    Raises on any failure so Airflow marks the task red.
    """
    run_pytest(
        "tests/test_raw_data_integration.py", "integration",
        env={"VIGIL_START_YEAR": str(START_YEAR),
             "VIGIL_END_YEAR": str(END_YEAR)},
    )


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

def build_failure_message(dag_id: str, run_id: str, failed_task_ids: list[str],
                          url: str) -> tuple[str, str]:
    """Build the email subject and HTML body. Pure function, easy to test."""
    items = "".join(f"<li>{t}</li>" for t in failed_task_ids) or \
        "<li>unknown (see Airflow UI)</li>"
    subject = f"[Vigil-MD] FAILED: {dag_id}"
    html = (
        f"<p>DAG <b>{dag_id}</b> failed.</p>"
        f"<p>Run: <code>{run_id}</code></p>"
        f"<p>Failed tasks:</p><ul>{items}</ul>"
        f'<p><a href="{url}">Open in Airflow</a></p>'
    )
    return subject, html


def alert_on_failure(**context):
    """Runs when any upstream task fails. Sends email alert."""
    import smtplib
    from email.mime.text import MIMEText

    ti = context["ti"]
    dag_run = context.get("dag_run")
    run_id = dag_run.run_id if dag_run else "unknown"

    url = (f"{os.environ.get('VIGIL_AIRFLOW_URL', 'http://localhost:8080')}"
           f"/dags/{ti.dag_id}/grid")

    log.error("PIPELINE FAILURE in run %s", run_id)

    to = os.environ.get("VIGIL_ALERT_EMAIL_TO")
    smtp_user = os.environ.get("AIRFLOW__SMTP__SMTP_USER")
    smtp_password = os.environ.get("AIRFLOW__SMTP__SMTP_PASSWORD")

    if not to:
        log.warning("VIGIL_ALERT_EMAIL_TO not set, skipping email")
        return
    if not smtp_user or not smtp_password:
        log.warning("SMTP credentials not set, skipping email")
        return

    subject = f"[Vigil-MD] FAILED: {ti.dag_id}"
    html = (
        f"<p>DAG <b>{ti.dag_id}</b> failed.</p>"
        f"<p>Run: <code>{run_id}</code></p>"
        f'<p><a href="{url}">Open in Airflow</a></p>'
    )

    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.ehlo()
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.send_message(msg)
        log.info("Alert email sent to %s", to)
    except Exception:
        log.exception("Failed to send alert email")

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
    #from airflow.utils.task_group import TaskGroup

        # ── Fetch (4 parallel tasks) ──────────────────────────────────────────────
    with TaskGroup("fetch", dag=dag) as fetch_group:
        fetch_enforcement = PythonOperator(
            task_id="fetch_enforcement",
            python_callable=fetch_one_source,
            op_kwargs={"source": "enforcement"},
            retries=3,
        )
        fetch_recall = PythonOperator(
            task_id="fetch_recall",
            python_callable=fetch_one_source,
            op_kwargs={"source": "recall"},
            retries=3,
        )
        fetch_maude = PythonOperator(
            task_id="fetch_maude",
            python_callable=fetch_one_source,
            op_kwargs={"source": "maude"},
            retries=5,
        )
        fetch_gudid = PythonOperator(
            task_id="fetch_gudid",
            python_callable=fetch_one_source,
            op_kwargs={"source": "gudid"},
            retries=5,
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
    check >> [fetch_group, no_update]
    fetch_group >> validate >> summary
    [fetch_group, validate, summary] >> alert