"""Vigil MD — Daily loading DAG: GCS → RAW → STAGING → PROD

Not scheduled. vigil_md_acquisition starts it with its last task
(trigger_load), only after acquisition is done, and passes
conf {"ingest_date": "<acquisition ds>"}. See docs/WAREHOUSE_PLAN.md (Step 10).

Tasks:
  1. load.*            — per source: unzip → load_raw → staging → merge
                         (--scope changed; pool vigil_warehouse, one BigQuery
                         writer at a time). A source with nothing pending is a
                         quick no-op.
  2. alert_on_failure  — triggered only if any task fails

Manual trigger: set the ingest_date param (YYYY-MM-DD), or leave it empty to
use the run's own date. The full load / backfill is vigil_md_full_load.py.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.sdk import Param, TaskGroup
from airflow.providers.standard.operators.python import PythonOperator
from airflow.task.trigger_rule import TriggerRule

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REPO       = Path(__file__).resolve().parents[1]
PYTHON     = sys.executable
BUCKET     = "vigil-md-raw-nd"
PROJECT    = "vigil-md-1792696816"
WAREHOUSE_POOL = "vigil_warehouse"     # 1 slot: one BigQuery writer at a time
SOURCES    = ["enforcement", "recall", "maude", "gudid"]

default_args = {
    "owner":            "nainica",
    "retries":          2,
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
    if result.returncode != 0:
        raise RuntimeError(
            f"{script} failed (exit {result.returncode})\n{result.stderr}"
        )
    return result.stdout


WAREHOUSE_STEPS = {
    # step: (script, takes --bucket, takes --scope)
    "unzip":    ("scripts/warehouse/unzip_to_ndjson.py", True,  True),
    "load_raw": ("scripts/warehouse/load_raw_to_bq.py",  True,  True),
    "staging":  ("scripts/warehouse/build_staging.py",   False, False),
    "merge":    ("scripts/warehouse/merge_prod.py",      False, False),
}


def warehouse_args(step: str, source: str, ingest_date: str,
                   scope: str = "changed") -> list[str]:
    """CLI args for one warehouse script. Pure function."""
    script, takes_bucket, takes_scope = WAREHOUSE_STEPS[step]
    args = [script, "--source", source, "--ingest-date", ingest_date]
    if step != "unzip":
        args += ["--project", PROJECT]
    if takes_bucket:
        args += ["--bucket", BUCKET]
    if takes_scope:
        args += ["--scope", scope]
    return args


def resolve_ingest_date(context: dict) -> str:
    """ingest_date from the trigger conf / param, else the run's ds. Pure function.

    Reads dag_run.conf directly too, so it does not depend on the
    dag_run_conf_overrides_params setting.
    """
    conf = getattr(context.get("dag_run"), "conf", None) or {}
    value = (conf.get("ingest_date")
             or (context.get("params") or {}).get("ingest_date")
             or context.get("ds"))
    if not value:
        raise ValueError("No ingest_date: trigger with conf {'ingest_date': 'YYYY-MM-DD'}")
    return date.fromisoformat(str(value)).isoformat()


# ── Tasks ─────────────────────────────────────────────────────────────────────

def run_load_step(step: str, source: str, **context):
    """Run one warehouse script for one source."""
    ingest_date = resolve_ingest_date(context)
    run_script(*warehouse_args(step, source, ingest_date))
    log.info("%s %s completed successfully (ingest_date %s)", step, source, ingest_date)


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
    dag_id="vigil_md_load",
    description="Vigil MD — Load GCS → RAW → STAGING → PROD (started by vigil_md_acquisition)",
    default_args=default_args,
    start_date=datetime(2026, 10, 1),
    schedule=None,          # started by vigil_md_acquisition.trigger_load
    catchup=False,
    max_active_runs=1,
    params={
        "ingest_date": Param(None, type=["null", "string"],
                             description="YYYY-MM-DD. Set by trigger_load; empty = the run's date"),
    },
    tags=["vigil-md", "warehouse", "load"],
) as dag:

    # One chain per source; chains are independent (a MAUDE failure does not
    # block recall). The 1-slot pool is shared with vigil_md_full_load.
    with TaskGroup("load", dag=dag) as load_group:
        for source in SOURCES:
            chain = [
                PythonOperator(
                    task_id=f"{step}_{source}",
                    python_callable=run_load_step,
                    op_kwargs={"step": step, "source": source},
                    pool=WAREHOUSE_POOL,
                )
                for step in WAREHOUSE_STEPS
            ]
            for upstream, downstream in zip(chain, chain[1:]):
                upstream >> downstream

    alert = PythonOperator(
        task_id="alert_on_failure",
        python_callable=alert_on_failure,
        trigger_rule=TriggerRule.ONE_FAILED,
    )

    load_group >> alert
