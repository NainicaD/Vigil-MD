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
sync_bundle       finds the newest published model bundle, downloads and checks it if it is
                  not on disk yet, and returns its folder (first task of the matching DAGs)
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
                          workflow: str, package: str = "matching-same-device") -> tuple[str, str]:
    """The e-mail subject and HTML body when retraining is started. Pure function."""
    from html import escape  # pylint: disable=import-outside-toplevel
    runs = f"https://github.com/{repo}/actions/workflows/{workflow}"
    subject = f"[Vigil-MD] Retraining started: {reason}"
    html = (f"<p>The matching model is being retrained: <b>{escape(reason)}</b>.</p>"
            f"<p>Found by <b>{dag_id}</b>, run <code>{run_id}</code>. The workflow trains from "
            f"<code>{escape(ref)}</code> and publishes a new model to <code>{escape(package)}</code> "
            f"only if it passes the model gate; "
            f"a second e-mail reports the result.</p>"
            f'<p><a href="{runs}">Workflow runs on GitHub</a> · <a href="{airflow_url(dag_id)}">Airflow</a></p>')
    return subject, html


def retrain_if_needed(reason: str | None, context: dict, ref: str, metrics: dict,
                      package: str | None = None, semver: str | None = None) -> str:
    """Start model-training.yml when `reason` is set, at most once per cooldown. Returns the outcome:
    "not_needed", "cooldown" or "started". Each outcome but "not_needed" leaves an ELK record
    matching_retrain_triggered and is logged. Raises (the task fails, so the failure e-mail goes out)
    when retraining is needed but the token is missing or GitHub refuses."""
    from vigil_checks import (  # pylint: disable=import-outside-toplevel
        RETRAIN_COOLDOWN_DAYS, RETRAIN_PACKAGE, RETRAIN_REPO, RETRAIN_SEMVER, RETRAIN_WORKFLOW,
        dispatch_body, recent_run)
    package, semver = package or RETRAIN_PACKAGE, semver or RETRAIN_SEMVER
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
    fields = {"reason": reason, "dag_id": dag_id, "run_id": run_id, "ref": ref, "package": package,
              **metrics}
    if last:
        log.warning("retraining needed (%s) but a run started %s (%s): once per %d days, not started",
                    reason, last["created_at"], last.get("html_url"), RETRAIN_COOLDOWN_DAYS)
        send_to_elk("matching_retrain_triggered", {**fields, "outcome": "cooldown",
                                                   "last_run_url": last.get("html_url")})
        return "cooldown"
    start_workflow(token, RETRAIN_REPO, RETRAIN_WORKFLOW, dispatch_body(ref, reason, package, semver))
    log.warning("retraining started (%s): %s on %s, publishing to %s", reason, RETRAIN_WORKFLOW, ref, package)
    send_to_elk("matching_retrain_triggered", {**fields, "outcome": "started"})
    send_email(*build_retrain_message(reason, dag_id, run_id, ref, RETRAIN_REPO, RETRAIN_WORKFLOW, package))
    return "started"


# ── ELK records from the matching DAGs ────────────────────────────────────────

# ── Model bundle pick-up (Model Deployment guidelines 5.4, the redeploy) ──────
# Artifact Registry is reached through its web API with the application-default credentials
# that docker-compose mounts (~/.config/gcloud). The Airflow image has no gcloud CLI.
AR_API = "https://artifactregistry.googleapis.com"
AR_REPOSITORY = "projects/vigil-md-1792696816/locations/us-central1/repositories/vigil-md-models"
AR_TIMEOUT_S = 120
BUNDLE_PACKAGE = "matching-same-device"


def _registry_session():
    """An HTTP session that signs each request with the mounted Google credentials."""
    import google.auth  # pylint: disable=import-outside-toplevel
    from google.auth.transport.requests import AuthorizedSession  # pylint: disable=import-outside-toplevel
    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def published_versions(package: str, session) -> list[str]:
    """The version names of a package in Artifact Registry, e.g. ["v1.0.0", "v1.1.0"]."""
    from urllib.parse import quote  # pylint: disable=import-outside-toplevel
    url = f"{AR_API}/v1/{AR_REPOSITORY}/packages/{quote(package, safe='')}/versions"
    names = []
    page_token = None
    while True:                                   # the API answers in pages
        params = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        r = session.get(url, params=params, timeout=AR_TIMEOUT_S)
        r.raise_for_status()
        body = r.json()
        # each name is the full path, ".../packages/<package>/versions/v1.1.0": keep the last part
        names += [v["name"].rsplit("/", 1)[-1] for v in body.get("versions", [])]
        page_token = body.get("nextPageToken")
        if not page_token:
            return names


def download_bundle_file(package: str, version: str, file_name: str, dest: Path, session) -> None:
    """Download one file of a published bundle to dest. In a generic repository a file is
    named <package>:<version>:<file name>."""
    from urllib.parse import quote  # pylint: disable=import-outside-toplevel
    file_id = quote(f"{package}:{version}:{file_name}", safe="")
    r = session.get(f"{AR_API}/download/v1/{AR_REPOSITORY}/files/{file_id}:download",
                    params={"alt": "media"}, timeout=AR_TIMEOUT_S)
    r.raise_for_status()
    dest.write_bytes(r.content)


def _read_manifest(folder: Path) -> dict:
    """manifest.json of a bundle folder, or {} when it is missing (bundle_problems then fails
    every file)."""
    import json  # pylint: disable=import-outside-toplevel
    path = folder / "manifest.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _newest_local_bundle(package: str, models: Path) -> str | None:
    """The newest bundle version of `package` already on disk that passes the sha256 check."""
    from vigil_checks import bundle_problems, newest_version  # pylint: disable=import-outside-toplevel
    if not models.exists():
        return None
    prefix = f"{package}-"
    good = []
    for folder in models.iterdir():
        # "matching-same-device-v1.1.0" -> "v1.1.0". A folder of another package
        # ("matching-same-device-test-v0.0.1" -> "test-v0.0.1") is not a version name, so it is ignored.
        if folder.is_dir() and folder.name.startswith(prefix):
            if not bundle_problems(folder, _read_manifest(folder)):
                good.append(folder.name[len(prefix):])
    return newest_version(good)


def sync_bundle(package: str = BUNDLE_PACKAGE, session=None, root: Path = REPO) -> dict:
    """Make sure the newest published bundle of `package` is on disk and intact, and say which one
    to use. Returns {"bundle": folder relative to Data-Pipeline/, "version": "v1.1.3",
    "new": True if it was downloaded just now}.

    - Already on disk and intact: used as it is, no download.
    - Not on disk: the files are downloaded into a temporary folder, checked against manifest.json,
      and only then renamed to the real folder, so a half-finished download is never used.
    - Artifact Registry cannot be reached (no network, no login): the newest intact bundle already
      on disk is used, with a warning. With none on disk, this fails.
    - A damaged bundle (a sha256 that differs from manifest.json) always fails; it is never used.
    root: the Data-Pipeline folder (tests pass a temporary one)."""
    import shutil  # pylint: disable=import-outside-toplevel
    from vigil_checks import BUNDLE_FILES, MATCHING_DIR, bundle_problems, newest_version  # pylint: disable=import-outside-toplevel

    models_rel = f"{MATCHING_DIR}/models"           # relative to Data-Pipeline/, as the bundle param
    models = root / models_rel

    def result(version: str, new: bool) -> dict:
        return {"bundle": f"{models_rel}/{package}-{version}", "version": version, "new": new}

    def fall_back(error: Exception) -> dict:
        local = _newest_local_bundle(package, models)
        if local is None:
            raise RuntimeError(f"Artifact Registry could not be reached ({error}) and no intact "
                               f"{package} bundle is on disk in {models_rel}") from error
        log.warning("Artifact Registry could not be reached (%s): using %s, already on disk",
                    error, local)
        return result(local, new=False)

    # 1. which version is the newest published one?
    try:
        session = session or _registry_session()
        version = newest_version(published_versions(package, session))
    except Exception as e:  # pylint: disable=broad-except  # network, login or API error
        return fall_back(e)
    if version is None:
        raise RuntimeError(f"Artifact Registry has no v<MAJOR>.<MINOR>.<PATCH> version of {package}")

    # 2. already on disk? Then check it and use it.
    folder = models / f"{package}-{version}"
    if folder.exists():
        problems = bundle_problems(folder, _read_manifest(folder))
        if problems:
            raise RuntimeError(f"{folder.name} on disk is damaged, not used: {problems}")
        log.info("bundle %s is on disk and intact", folder.name)
        return result(version, new=False)

    # 3. download into a temporary folder
    partial = models / f".{package}-{version}.partial"
    shutil.rmtree(partial, ignore_errors=True)       # left over from an earlier, interrupted try
    partial.mkdir(parents=True)
    try:
        download_bundle_file(package, version, "manifest.json", partial / "manifest.json", session)
        for file_name in BUNDLE_FILES:
            download_bundle_file(package, version, file_name, partial / file_name, session)
    except Exception as e:  # pylint: disable=broad-except
        shutil.rmtree(partial, ignore_errors=True)
        return fall_back(e)

    # 4. check it; only an intact bundle gets its real name
    problems = bundle_problems(partial, _read_manifest(partial))
    if problems:
        shutil.rmtree(partial, ignore_errors=True)
        raise RuntimeError(f"downloaded {package} {version} is damaged, not used: {problems}")
    partial.rename(folder)
    log.warning("downloaded new bundle %s from Artifact Registry", folder.name)
    return result(version, new=True)


def choose_bundle(**context) -> dict:
    """Task sync_bundle, the first task of both matching DAGs. A folder in the param bundle (set by
    hand, e.g. a rollback) wins and nothing is downloaded; otherwise sync_bundle() picks the newest
    published version of the param bundle_package. The returned dict goes to XCom."""
    params = context["params"]
    if params.get("bundle"):
        log.warning("bundle set by hand: %s (the newest published version is not checked)", params["bundle"])
        return {"bundle": params["bundle"], "version": None, "new": False}
    return sync_bundle(params.get("bundle_package") or BUNDLE_PACKAGE)


def bundle_folder(context) -> str:
    """The bundle folder a matching task uses: the param bundle if it was set by hand, else the
    folder task sync_bundle chose (XCom)."""
    return context["params"].get("bundle") or context["ti"].xcom_pull(task_ids="sync_bundle")["bundle"]


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
