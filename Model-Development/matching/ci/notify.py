"""The result e-mail of a training run (model-training.yml, Model Deployment guidelines 5.5).

One e-mail per run of the train job: published, champion kept (the gate failed), no change (s11
skipped it), not published (a run without publish), or failed before the gate. Read from the step
outcomes (GATE_OUTCOME, PUBLISH_OUTCOME), s11's output (--publish-log) and the gate's checks
(--gate-json). Sent through smtp.gmail.com:587 with SMTP_USER, SMTP_PASSWORD (a Gmail app
password) and ALERT_EMAIL_TO; without them it is only printed. Exit 0 always: an e-mail problem
must not fail a training run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
from email.mime.text import MIMEText
from html import escape as html_escape
from pathlib import Path


def outcome(gate: str, publish: str, publish_requested: bool, log: str) -> tuple[str, str]:
    """(status, detail) of one run. gate / publish: the steps' outcomes (success, failure, skipped,
    cancelled, or "" when the step did not run). Pure function."""
    if gate == "failure":
        return "champion kept", "the new model did not pass the model gate, so nothing was published"
    if gate != "success":
        return "failed", "the run stopped before the model gate; see the run log"
    if not publish_requested:
        return "not published", "the gate passed; this run was started without publish"
    if publish == "failure":
        last = next((line for line in reversed(log.splitlines()) if line.strip()), "")
        return "failed", "the gate passed, but publishing failed" + (f": {last.strip()[:300]}" if last else "; see the run log")
    if publish != "success":
        return "failed", f"the gate passed, but the publish step did not run ({publish or 'not run'})"
    m = re.search(r"^published (\S+) (v\d+\.\d+\.\d+)", log, re.M)
    if m:
        return "published", f"{m.group(1)} {m.group(2)} is published; the DAGs use it from their next run"
    m = re.search(r"^not published: same model.*$|^.* is already published: skipped$", log, re.M)
    if m:
        return "no change", m.group(0)
    return "failed", "the publish step succeeded but its output names no outcome; see the run log"


def failed_checks(gate_json: Path) -> list[str]:
    """The failed gate checks (ci/gate.py's gate.json), or [] without the file."""
    if not gate_json.exists():
        return []
    g = json.loads(gate_json.read_text())
    return [f"{c['name']}: {c.get('detail', '')}".rstrip(": ") for c in g.get("checks", [])
            if c.get("status") == "FAIL"]


def message(status: str, detail: str, checks: list[str], reason: str, ref: str, run_url: str) -> tuple[str, str]:
    """Subject and HTML body. Pure function."""
    e = html_escape
    subject = f"[Vigil-MD] Matching model training: {status}"
    why = f"<p>Started because: <b>{e(reason)}</b></p>" if reason else ""
    fails = ("<p>Failed gate checks:</p><ul>" + "".join(f"<li>{e(c)}</li>" for c in checks) + "</ul>") if checks else ""
    body = (f"<p><b>{e(status)}</b>: {e(detail)}.</p>{why}{fails}"
            f"<p>Branch <code>{e(ref)}</code>. <a href=\"{e(run_url)}\">The run on GitHub</a></p>")
    return subject, body


def send(subject: str, html: str) -> bool:
    user, password, to = (os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"))
    if not (user and password and to):
        print("e-mail skipped: SMTP_USER, SMTP_PASSWORD or ALERT_EMAIL_TO is not set")
        return False
    msg = MIMEText(html, "html")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
    except Exception as e:  # noqa: BLE001 - reported, never raised
        print(f"e-mail not sent: {type(e).__name__}")
        return False
    print(f"e-mail sent: {subject}")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--publish-log", type=Path, help="s11's output")
    ap.add_argument("--gate-json", type=Path, default=Path("data/scores/gate.json"))
    ap.add_argument("--summary", type=Path, help="append the result as Markdown (GITHUB_STEP_SUMMARY)")
    a = ap.parse_args(argv)
    env = os.environ.get
    log = a.publish_log.read_text() if a.publish_log and a.publish_log.exists() else ""
    status, detail = outcome(env("GATE_OUTCOME", ""), env("PUBLISH_OUTCOME", ""),
                             env("PUBLISH_REQUESTED", "false") == "true", log)
    checks = failed_checks(a.gate_json) if status == "champion kept" else []
    print(f"{status}: {detail}")
    if a.summary:
        with a.summary.open("a") as f:
            f.write(f"\n**Result: {status}**: {detail}\n" + "".join(f"\n- {c}" for c in checks) + "\n")
    send(*message(status, detail, checks, env("REASON", ""), env("REF_NAME", ""), env("RUN_URL", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
