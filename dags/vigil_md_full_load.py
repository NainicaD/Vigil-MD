"""Vigil MD — Full load DAG (manual): GCS → RAW → STAGING → PROD

Trigger by hand for the first load, a backfill, a schema change or a recovery.
See docs/WAREHOUSE_PLAN.md (Step 8) and the Runbook.

Params:
  sources       which sources to load (default: all 4)
  refetch       first re-pull from the FDA with fetch_sources.py --mode full
                (default false: use the zips already in GCS)
  rebuild_prod  replace every prod row from all staging history
                (merge_prod.py --rebuild --yes-rebuild; default false: MERGE)

Tasks:
  1. guard_no_acquisition_run — fail if vigil_md_acquisition or vigil_md_load is running
  2. refetch                  — fetch_sources.py --mode full (only if refetch)
  3. load.*                   — per source: unzip --scope all → load_raw --scope all
                                --mode full → staging → merge
  4. alert_on_failure         — triggered only if any task fails

Every task that writes GCS state or BigQuery uses the 1-slot vigil_warehouse
pool, shared with vigil_md_acquisition.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.sdk import Param, TaskGroup
from airflow.sdk.exceptions import AirflowSkipException

sys.path.insert(0, str(Path(__file__).resolve().parent))   # dags/ (shared modules)
from vigil_checks import (  # noqa: E402
    CHECK_SCRIPTS, EXIT_DATA_FAIL, VALIDATE_EXTRA_ARGS, DataCheckFailed, warn_only)
from airflow.providers.standard.operators.python import PythonOperator
from airflow.task.trigger_rule import TriggerRule

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REPO       = Path(__file__).resolve().parents[1]
PYTHON     = sys.executable
BUCKET     = "vigil-md-raw-nd"
PROJECT    = "vigil-md-1792696816"
START_YEAR = 2016
END_YEAR   = 2026
WAREHOUSE_POOL = "vigil_warehouse"
SOURCES    = ["enforcement", "recall", "maude", "gudid"]
# Daily DAGs that write the same GCS state and BigQuery tables
DAILY_DAG_IDS = ["vigil_md_acquisition", "vigil_md_load"]

default_args = {
    "owner":            "nainica",
    "retries":          1,
    "retry_delay":      timedelta(minutes=5),
    "email_on_failure": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

# TODO: move to shared module (copied from vigil_md_acquisition.py:run_script)
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
    if result.returncode == EXIT_DATA_FAIL:
        raise DataCheckFailed(
            f"{script} found a data problem (exit {EXIT_DATA_FAIL})\n{result.stderr}"
        )
    if result.returncode != 0:
        raise RuntimeError(
            f"{script} failed (exit {result.returncode})\n{result.stderr}"
        )
    return result.stdout


def full_load_args(step: str, source: str, ingest_date: str,
                   rebuild: bool = False) -> list[str]:
    """CLI args for one full-load script. Pure function."""
    if step == "unzip":
        return ["scripts/warehouse/unzip_to_ndjson.py", "--bucket", BUCKET,
                "--source", source, "--scope", "all", "--ingest-date", ingest_date]
    if step == "load_raw":
        return ["scripts/warehouse/load_raw_to_bq.py", "--project", PROJECT,
                "--bucket", BUCKET, "--source", source, "--scope", "all",
                "--ingest-date", ingest_date, "--mode", "full"]
    if step == "staging":
        return ["scripts/warehouse/build_staging.py", "--project", PROJECT,
                "--source", source, "--ingest-date", ingest_date]
    if step in ("validate", "check"):
        args = [CHECK_SCRIPTS[step], "--project", PROJECT,
                "--source", source, "--ingest-date", ingest_date]
        return args + (VALIDATE_EXTRA_ARGS if step == "validate" else [])
    if step == "merge":
        args = ["scripts/warehouse/merge_prod.py", "--project", PROJECT,
                "--source", source, "--ingest-date", ingest_date]
        return args + (["--rebuild", "--yes-rebuild"] if rebuild else [])
    raise ValueError(f"unknown step {step}")


LOAD_STEPS = ["unzip", "load_raw", "staging", "validate", "check", "merge"]


def selected_sources(params: dict) -> list[str]:
    """Validate the sources param. Pure function."""
    chosen = list(params.get("sources") or [])
    unknown = sorted(set(chosen) - set(SOURCES))
    if unknown or not chosen:
        raise ValueError(f"sources must be a non-empty subset of {SOURCES}, got {chosen}")
    return chosen


# ── Tasks ─────────────────────────────────────────────────────────────────────

def guard_no_acquisition_run(**context):
    """Fail if a daily DAG has a run in progress (they write the same state)."""
    selected_sources(context["params"])            # fail early on a bad param
    for daily in DAILY_DAG_IDS:
        n = context["ti"].get_dr_count(dag_id=daily, states=["running", "queued"])
        if n:
            raise RuntimeError(
                f"{daily} has {n} run(s) in progress. Wait for it to "
                "finish, or pause it, then trigger the full load again.")
    log.info("No run in progress for %s", ", ".join(DAILY_DAG_IDS))


def refetch(**context):
    """Re-pull the selected sources with --mode full, if the refetch param is set."""
    params = context["params"]
    if not params.get("refetch"):
        log.info("refetch=false — using the zips already in GCS")
        return
    for source in selected_sources(params):
        run_script(
            "scripts/acquisition/fetch_sources.py",
            "--bucket",     BUCKET,
            "--source",     source,
            "--start-year", str(START_YEAR),
            "--end-year",   str(END_YEAR),
            "--mode",       "full",
        )
        log.info("%s full fetch completed", source)


def load_date(context: dict) -> str:
    """The run's date (YYYY-MM-DD). Uses ds when the run has a logical date;
    a run triggered without one (e.g. from the CLI) has no ds, so fall back to
    the time the run was created."""
    if context.get("ds"):
        return context["ds"]
    return context["dag_run"].run_after.date().isoformat()


def run_load_step(step: str, source: str, **context):
    params = context["params"]
    if source not in selected_sources(params):
        raise AirflowSkipException(f"{source} not in sources param")
    rebuild = bool(params.get("rebuild_prod")) and step == "merge"
    try:
        run_script(*full_load_args(step, source, load_date(context), rebuild))
    except DataCheckFailed:
        if warn_only(step):
            log.warning("%s %s found data problems (warn-only, CHECKS_ENFORCE is False): "
                        "the merge continues. See the check output above.", step, source)
            return
        raise
    log.info("%s %s completed%s", step, source, " (rebuild)" if rebuild else "")


# TODO: move to shared module (copied from vigil_md_acquisition.py:alert_on_failure)
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
    dag_id="vigil_md_full_load",
    description="Vigil MD — Manual full load: GCS → RAW → STAGING → PROD",
    default_args=default_args,
    start_date=datetime(2026, 10, 1),
    schedule=None,          # manual trigger only
    catchup=False,
    max_active_runs=1,
    params={
        "sources":      Param(SOURCES, type="array",
                              description=f"Subset of {SOURCES}"),
        "refetch":      Param(False, type="boolean",
                              description="Re-pull from the FDA first (fetch_sources.py --mode full)"),
        "rebuild_prod": Param(False, type="boolean",
                              description="Replace every prod row from all staging history"),
    },
    tags=["vigil-md", "warehouse", "full-load"],
) as dag:

    guard = PythonOperator(
        task_id="guard_no_acquisition_run",
        python_callable=guard_no_acquisition_run,
        retries=0,
    )

    fetch = PythonOperator(
        task_id="refetch",
        python_callable=refetch,
        pool=WAREHOUSE_POOL,
    )

    # One chain per source; sources not in the param are skipped.
    with TaskGroup("load", dag=dag) as load_group:
        for source in SOURCES:
            chain = [
                PythonOperator(
                    task_id=f"{step}_{source}",
                    python_callable=run_load_step,
                    op_kwargs={"step": step, "source": source},
                    pool=WAREHOUSE_POOL,
                )
                for step in LOAD_STEPS
            ]
            for upstream, downstream in zip(chain, chain[1:]):
                upstream >> downstream
            fetch >> chain[0]

    alert = PythonOperator(
        task_id="alert_on_failure",
        python_callable=alert_on_failure,
        trigger_rule=TriggerRule.ONE_FAILED,
    )

    guard >> fetch
    [guard, fetch, load_group] >> alert
