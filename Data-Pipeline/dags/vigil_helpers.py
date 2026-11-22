"""Task helpers shared by the three Vigil MD DAGs (docs/issue.md #8, #10, #14).

Used by vigil_md_acquisition, vigil_md_load and vigil_md_full_load, so a fix is
made once. Not a DAG: listed in dags/.airflowignore so Airflow does not parse it
for DAGs.

run_script        runs a repo script and streams its output into the task log
                  line by line (live), keeping each line's log level
alert_on_failure  e-mail task (trigger_rule one_failed) that names the failed tasks
mark_run_failed   task (trigger_rule one_failed) that fails, so the run's state is "failed"
                  even when the last tasks (all_done summaries/links, the alert) succeed
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path

from airflow.sdk.exceptions import AirflowFailException

from vigil_checks import EXIT_DATA_FAIL, DataCheckFailed

log = logging.getLogger(__name__)

REPO   = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
TAIL_LINES = 100      # last output lines kept for the exception message

# A line from scripts/logger.py: "2026-11-04 07:36:49,861 WARNING  merge_prod — …"
_LEVEL = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (WARNING|ERROR|CRITICAL)\b")


def line_level(line: str) -> int:
    """Log level for one output line: our logger's WARNING/ERROR/CRITICAL, else INFO."""
    m = _LEVEL.match(line)
    return getattr(logging, m.group(1)) if m else logging.INFO


def run_script(script: str, *args: str) -> None:
    """Run a repo script as a subprocess, logging its output while it runs.

    stdout and stderr are merged (one pipe, real line order); PYTHONUNBUFFERED
    makes the script's print() output arrive line by line instead of at the end.
    Raises DataCheckFailed on exit 3 (a data problem: no retry), RuntimeError on
    any other non-zero exit. If this task is stopped, the script is killed too.
    """
    cmd = [PYTHON, str(REPO / script), *args]
    log.info("Running: %s", " ".join(cmd))
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    proc = subprocess.Popen(
        cmd, cwd=str(REPO), env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )
    with proc:          # on exit: closes the pipe and waits for the script
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                log.log(line_level(line), "%s", line)
                tail.append(line)
        except BaseException:
            proc.kill()     # like subprocess.run: never leave the script running.
            raise           # Inside the with: Popen.__exit__ would otherwise wait for it.
    output = "\n".join(tail)
    if proc.returncode == EXIT_DATA_FAIL:
        raise DataCheckFailed(f"{script} found a data problem (exit {EXIT_DATA_FAIL})\n{output}")
    if proc.returncode != 0:
        raise RuntimeError(f"{script} failed (exit {proc.returncode})\n{output}")


# ── Alert ─────────────────────────────────────────────────────────────────────

def failed_task_ids(ti, run_id: str) -> list[str]:
    """Tasks of this run in state "failed" so far. [] if the lookup fails.

    ti.get_task_states asks the Airflow 3 supervisor ({run_id: {task_id: state}});
    tasks cannot query the metadata database directly.
    """
    try:
        states = ti.get_task_states(dag_id=ti.dag_id, run_ids=[run_id]).get(run_id, {})
    except Exception:
        log.exception("Could not read the task states of run %s", run_id)
        return []
    return sorted(task_id for task_id, state in states.items() if state == "failed")


def build_failure_message(dag_id: str, run_id: str, failed_task_ids: list[str],
                          url: str) -> tuple[str, str]:
    """Build the email subject and HTML body. Pure function, easy to test."""
    items = "".join(f"<li>{t}</li>" for t in failed_task_ids) or \
        "<li>unknown (see Airflow UI)</li>"
    names = f" ({', '.join(failed_task_ids)})" if failed_task_ids else ""
    subject = f"[Vigil-MD] FAILED: {dag_id}{names}"
    html = (
        f"<p>DAG <b>{dag_id}</b> failed.</p>"
        f"<p>Run: <code>{run_id}</code></p>"
        f"<p>Failed tasks so far (others may still be running):</p><ul>{items}</ul>"
        f'<p><a href="{url}">Open in Airflow</a></p>'
    )
    return subject, html


def alert_on_failure(**context):
    """Runs when any upstream task fails. Sends email alert naming the failed tasks."""
    import smtplib
    from email.mime.text import MIMEText

    ti = context["ti"]
    dag_run = context.get("dag_run")
    run_id = dag_run.run_id if dag_run else "unknown"

    url = (f"{os.environ.get('VIGIL_AIRFLOW_URL', 'http://localhost:8080')}"
           f"/dags/{ti.dag_id}/grid")

    failed = failed_task_ids(ti, run_id) if dag_run else []
    log.error("PIPELINE FAILURE in run %s: %s", run_id, ", ".join(failed) or "unknown tasks")

    to = os.environ.get("VIGIL_ALERT_EMAIL_TO")
    smtp_user = os.environ.get("AIRFLOW__SMTP__SMTP_USER")
    smtp_password = os.environ.get("AIRFLOW__SMTP__SMTP_PASSWORD")

    if not to:
        log.warning("VIGIL_ALERT_EMAIL_TO not set, skipping email")
        return
    if not smtp_user or not smtp_password:
        log.warning("SMTP credentials not set, skipping email")
        return

    subject, html = build_failure_message(ti.dag_id, run_id, failed, url)
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


# ── ELK records from the matching DAGs ────────────────────────────────────────

def send_to_elk(message: str, fields: dict) -> None:
    """One structured record through scripts/logger.py (Logstash at VIGIL_LOGSTASH_HOST)."""
    sys.path.insert(0, str(REPO))
    from scripts.logger import get_logger  # pylint: disable=import-outside-toplevel
    get_logger("vigil_md_matching").info(message, extra=fields)


def task_event_fields(context: dict) -> dict:
    """The fields of matching_task_failed / matching_task_retry. chunks_done: lines in the run's
    chunks.jsonl, i.e. scored chunks that a retry will not score again."""
    from vigil_checks import WORK_DIRS, matching_files  # pylint: disable=import-outside-toplevel
    ti, run_id = context["ti"], context["run_id"]
    chunks = Path(matching_files(run_id, WORK_DIRS[ti.dag_id])["chunks"])
    return {"dag_id": ti.dag_id, "task_id": ti.task_id, "run_id": run_id,
            "try_number": ti.try_number, "error": str(context.get("exception"))[:500],
            "chunks_done": len(chunks.read_text().splitlines()) if chunks.exists() else 0}


def matching_task_failed(context):
    """on_failure_callback: a failed task leaves a record in ELK, not only a red box in Airflow."""
    send_to_elk("matching_task_failed", task_event_fields(context))


def matching_task_retry(context):
    """on_retry_callback: each retry is visible in ELK."""
    send_to_elk("matching_task_retry", task_event_fields(context))


# ── Run state ─────────────────────────────────────────────────────────────────

def mark_run_failed(**context):
    """Fail on purpose when any task failed (trigger_rule one_failed).

    Airflow sets a run's state from its last tasks. build_summaries and build_links
    (all_done) and alert_on_failure succeed after a failed chain, so without this task
    the run showed "success" although e.g. MAUDE failed (2026-11-17). Skipped when no
    task fails. AirflowFailException: no retry.
    """
    ti = context["ti"]
    dag_run = context.get("dag_run")
    run_id = dag_run.run_id if dag_run else "unknown"
    failed = failed_task_ids(ti, run_id) if dag_run else []
    raise AirflowFailException(
        f"Run {run_id} is marked failed because a task failed: "
        f"{', '.join(failed) or 'see the grid'}")
