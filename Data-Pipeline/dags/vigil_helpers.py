"""Task helpers shared by the three Vigil MD DAGs (docs/issue.md #8, #10, #14).

Used by vigil_md_acquisition, vigil_md_load and vigil_md_full_load, so a fix is
made once. Not a DAG: listed in dags/.airflowignore so Airflow does not parse it
for DAGs.

run_script        runs a repo script and streams its output into the task log
                  line by line (live), keeping each line's log level
alert_on_failure  e-mail task (trigger_rule one_failed) that names the failed tasks
send_email        one HTML e-mail to VIGIL_ALERT_EMAIL_TO (used by the alert and the retrain trigger)
retrain_if_needed starts the GitHub workflow model-training.yml when a check found a reason,
                  at most once per 7 days (matching DAGs)
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
from datetime import datetime, timedelta, timezone
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


def send_email(subject: str, html: str) -> bool:
    """One HTML e-mail through smtp.gmail.com:587 to VIGIL_ALERT_EMAIL_TO. Skipped with a warning
    when the settings are missing; a send error is logged, not raised. True if sent."""
    import smtplib
    from email.mime.text import MIMEText

    to = os.environ.get("VIGIL_ALERT_EMAIL_TO")
    smtp_user = os.environ.get("AIRFLOW__SMTP__SMTP_USER")
    smtp_password = os.environ.get("AIRFLOW__SMTP__SMTP_PASSWORD")
    if not to:
        log.warning("VIGIL_ALERT_EMAIL_TO not set, skipping email")
        return False
    if not smtp_user or not smtp_password:
        log.warning("SMTP credentials not set, skipping email")
        return False

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
        log.info("Email sent to %s: %s", to, subject)
        return True
    except Exception:
        log.exception("Failed to send email: %s", subject)
        return False


def airflow_url(dag_id: str) -> str:
    return f"{os.environ.get('VIGIL_AIRFLOW_URL', 'http://localhost:8080')}/dags/{dag_id}/grid"


def alert_on_failure(**context):
    """Runs when any upstream task fails. Sends email alert naming the failed tasks."""
    ti = context["ti"]
    dag_run = context.get("dag_run")
    run_id = dag_run.run_id if dag_run else "unknown"

    failed = failed_task_ids(ti, run_id) if dag_run else []
    log.error("PIPELINE FAILURE in run %s: %s", run_id, ", ".join(failed) or "unknown tasks")
    send_email(*build_failure_message(ti.dag_id, run_id, failed, airflow_url(ti.dag_id)))


# ── Retraining trigger (GitHub workflow_dispatch) ─────────────────────────────

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT_S = 30


def _github(method: str, path: str, token: str, **kw):
    """One GitHub REST call. The token goes in the header only, never in a log or an error."""
    import requests  # pylint: disable=import-outside-toplevel
    return requests.request(method, f"{GITHUB_API}{path}", timeout=GITHUB_TIMEOUT_S, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}, **kw)


def dispatched_runs(token: str, repo: str, workflow: str, since: datetime) -> list[dict]:
    """The workflow's runs started by workflow_dispatch since `since` (GitHub API)."""
    r = _github("GET", f"/repos/{repo}/actions/workflows/{workflow}/runs", token, params={
        "event": "workflow_dispatch", "created": f">={since:%Y-%m-%d}", "per_page": 20})
    if r.status_code != 200:
        raise RuntimeError(f"GitHub: listing {workflow} runs failed (HTTP {r.status_code}): {r.text[:300]}")
    return r.json().get("workflow_runs", [])


def start_workflow(token: str, repo: str, workflow: str, body: dict) -> None:
    """workflow_dispatch: GitHub answers 204 and returns no run ID."""
    r = _github("POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches", token, json=body)
    if r.status_code != 204:
        raise RuntimeError(f"GitHub: starting {workflow} on {body['ref']} failed "
                           f"(HTTP {r.status_code}): {r.text[:300]}")


def build_retrain_message(reason: str, dag_id: str, run_id: str, ref: str, repo: str,
                          workflow: str) -> tuple[str, str]:
    """The e-mail subject and HTML body when retraining is started. Pure function."""
    from html import escape  # pylint: disable=import-outside-toplevel
    runs = f"https://github.com/{repo}/actions/workflows/{workflow}"
    subject = f"[Vigil-MD] Retraining started: {reason}"
    html = (f"<p>The matching model is being retrained: <b>{escape(reason)}</b>.</p>"
            f"<p>Found by <b>{dag_id}</b>, run <code>{run_id}</code>. The workflow trains from "
            f"<code>{ref}</code> and publishes a new model only if it passes the model gate; "
            f"a second e-mail reports the result.</p>"
            f'<p><a href="{runs}">Workflow runs on GitHub</a> · <a href="{airflow_url(dag_id)}">Airflow</a></p>')
    return subject, html


def retrain_if_needed(reason: str | None, context: dict, ref: str, metrics: dict) -> str:
    """Start model-training.yml when `reason` is set, at most once per cooldown. Returns the outcome:
    "not_needed", "cooldown" or "started". Each outcome but "not_needed" leaves an ELK record
    matching_retrain_triggered and is logged. Raises (the task fails, so the failure e-mail goes out)
    when retraining is needed but the token is missing or GitHub refuses."""
    from vigil_checks import (  # pylint: disable=import-outside-toplevel
        RETRAIN_COOLDOWN_DAYS, RETRAIN_REPO, RETRAIN_WORKFLOW, dispatch_body, recent_run)
    dag_id, run_id = context["ti"].dag_id, context["run_id"]
    if reason is None:
        log.info("no retraining needed: %s", metrics)
        return "not_needed"
    token = os.environ.get("VIGIL_GITHUB_TOKEN")
    if not token:
        raise AirflowFailException(f"retraining needed ({reason}) but VIGIL_GITHUB_TOKEN is not set "
                                   "in .env: start model-training by hand, or add the token")
    now = datetime.now(timezone.utc)
    last = recent_run(dispatched_runs(token, RETRAIN_REPO, RETRAIN_WORKFLOW,
                                      now - timedelta(days=RETRAIN_COOLDOWN_DAYS)), now)
    fields = {"reason": reason, "dag_id": dag_id, "run_id": run_id, "ref": ref, **metrics}
    if last:
        log.warning("retraining needed (%s) but a run started %s (%s): once per %d days, not started",
                    reason, last["created_at"], last.get("html_url"), RETRAIN_COOLDOWN_DAYS)
        send_to_elk("matching_retrain_triggered", {**fields, "outcome": "cooldown",
                                                   "last_run_url": last.get("html_url")})
        return "cooldown"
    start_workflow(token, RETRAIN_REPO, RETRAIN_WORKFLOW, dispatch_body(ref, reason))
    log.warning("retraining started (%s): %s on %s", reason, RETRAIN_WORKFLOW, ref)
    send_to_elk("matching_retrain_triggered", {**fields, "outcome": "started"})
    send_email(*build_retrain_message(reason, dag_id, run_id, ref, RETRAIN_REPO, RETRAIN_WORKFLOW))
    return "started"


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
