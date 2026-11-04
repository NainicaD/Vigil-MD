"""Tests for dags/vigil_helpers.py (docs/issue.md #8, #10, #14).

run_script runs real (tiny) Python scripts written to tmp_path; no repo script,
no GCP, no network. The alert tests use a fake task instance and no SMTP.
"""
import importlib.util
import logging
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "dags"
sys.path.insert(0, str(DAGS))

import vigil_helpers as H  # noqa: E402
from vigil_checks import DataCheckFailed  # noqa: E402

TS = "2026-11-04 07:36:49,861"


def script(tmp_path, body: str) -> str:
    path = tmp_path / "child.py"
    path.write_text(textwrap.dedent(body))
    return str(path)        # absolute: REPO / script keeps it as is


# ── line_level ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("line, level", [
    (f"{TS} WARNING  merge_prod — 1 check(s) FAILED", logging.WARNING),
    (f"{TS} ERROR    load_raw — boom", logging.ERROR),
    (f"{TS} CRITICAL x — y", logging.CRITICAL),
    (f"{TS} INFO     merge_prod — 21532241 staged rows", logging.INFO),
    ("PASS  dates   0 future", logging.INFO),
    ("FAIL  raw_to_staged_gap   WARNING in data text", logging.INFO),   # not our log format
    ("", logging.INFO),
])
def test_line_level(line, level):
    assert H.line_level(line) == level


# ── run_script ───────────────────────────────────────────────────────────────

def test_run_script_logs_every_line_in_order_with_its_level(tmp_path, caplog):
    child = script(tmp_path, f"""
        import sys
        print("first (stdout)")
        print("{TS} WARNING  child — second (stderr)", file=sys.stderr)
        print("third (stdout)")
    """)
    caplog.set_level(logging.INFO, logger=H.log.name)
    H.run_script(child)
    out = [(r.levelno, r.getMessage()) for r in caplog.records if not r.getMessage().startswith("Running:")]
    assert out == [(logging.INFO, "first (stdout)"),
                   (logging.WARNING, f"{TS} WARNING  child — second (stderr)"),
                   (logging.INFO, "third (stdout)")]


def test_run_script_child_is_unbuffered(tmp_path, caplog):
    """Without PYTHONUNBUFFERED, print() output would only arrive when the script ends."""
    child = script(tmp_path, 'import os; print("PYTHONUNBUFFERED=" + os.environ.get("PYTHONUNBUFFERED", ""))')
    caplog.set_level(logging.INFO, logger=H.log.name)
    H.run_script(child)
    assert "PYTHONUNBUFFERED=1" in caplog.text


@pytest.mark.parametrize("code, exc", [(3, DataCheckFailed), (1, RuntimeError), (2, RuntimeError)])
def test_run_script_raises_on_non_zero_exit_with_the_output_tail(tmp_path, code, exc):
    child = script(tmp_path, f'print("FAIL  raw_to_staged_gap"); raise SystemExit({code})')
    with pytest.raises(exc, match=rf"exit {code}\)\nFAIL  raw_to_staged_gap"):
        H.run_script(child)


def test_run_script_tail_keeps_the_last_lines(tmp_path):
    child = script(tmp_path, f'[print(i) for i in range({H.TAIL_LINES + 20})]; raise SystemExit(1)')
    with pytest.raises(RuntimeError) as e:
        H.run_script(child)
    lines = str(e.value).splitlines()[1:]
    assert len(lines) == H.TAIL_LINES and lines[-1] == str(H.TAIL_LINES + 19)


def test_run_script_kills_the_script_when_the_task_stops(tmp_path, monkeypatch):
    """A stopped task must not leave the script running (it could keep writing to BigQuery)."""
    pid_file = tmp_path / "pid"
    child = script(tmp_path, f"""
        import os, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        print("started", flush=True)
        time.sleep(60)
    """)

    def stop(*a, **k):
        if pid_file.exists():
            raise KeyboardInterrupt("task stopped")
    monkeypatch.setattr(H.log, "log", stop)
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        H.run_script(child)
    assert time.monotonic() - t0 < 10                 # did not wait for the 60 s sleep
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)         # the script is gone


def test_run_script_kills_the_script_on_any_error(tmp_path, monkeypatch):
    """Popen.__exit__ waits for the script on most exceptions, so the kill must come first."""
    child = script(tmp_path, 'import time; print("started", flush=True); time.sleep(60)')
    monkeypatch.setattr(H.log, "log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        H.run_script(child)
    assert time.monotonic() - t0 < 10


# ── alert ────────────────────────────────────────────────────────────────────

class FakeTI:
    dag_id = "vigil_md_load"

    def __init__(self, states=None, error=None):
        self.states, self.error, self.calls = states or {}, error, []

    def get_task_states(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return self.states


def test_failed_task_ids_keeps_only_failed():
    ti = FakeTI({"r1": {"load.check_maude": "failed", "load.merge_maude": "upstream_failed",
                        "load.unzip_gudid": "success", "load.load_raw_recall": "running",
                        "load.check_recall": "failed"}})
    assert H.failed_task_ids(ti, "r1") == ["load.check_maude", "load.check_recall"]
    assert ti.calls == [{"dag_id": "vigil_md_load", "run_ids": ["r1"]}]


def test_failed_task_ids_lookup_error_gives_empty_list():
    assert H.failed_task_ids(FakeTI(error=RuntimeError("no supervisor")), "r1") == []
    assert H.failed_task_ids(FakeTI({"other": {"a": "failed"}}), "r1") == []


def test_build_failure_message_names_the_tasks():
    subject, html = H.build_failure_message("vigil_md_load", "r1", ["load.check_maude"], "http://x")
    assert subject == "[Vigil-MD] FAILED: vigil_md_load (load.check_maude)"
    assert "<li>load.check_maude</li>" in html and "so far" in html and 'href="http://x"' in html


def test_build_failure_message_without_tasks():
    subject, html = H.build_failure_message("vigil_md_load", "r1", [], "http://x")
    assert subject == "[Vigil-MD] FAILED: vigil_md_load"
    assert "unknown (see Airflow UI)" in html


def test_alert_logs_the_failed_tasks_without_email_settings(monkeypatch, caplog):
    monkeypatch.delenv("VIGIL_ALERT_EMAIL_TO", raising=False)
    caplog.set_level(logging.INFO, logger=H.log.name)
    dag_run = type("DR", (), {"run_id": "r1"})()
    H.alert_on_failure(ti=FakeTI({"r1": {"load.check_maude": "failed"}}), dag_run=dag_run)
    assert "PIPELINE FAILURE in run r1: load.check_maude" in caplog.text
    assert "VIGIL_ALERT_EMAIL_TO not set" in caplog.text


# ── the 3 DAGs use the shared helpers (no copies) ────────────────────────────

@pytest.mark.parametrize("name", ["vigil_md_acquisition", "vigil_md_load", "vigil_md_full_load"])
def test_dags_use_the_shared_helpers(name):
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(f"{name}_helpers_test", DAGS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.run_script is H.run_script
    assert mod.dag.get_task("alert_on_failure").python_callable is H.alert_on_failure
    src = (DAGS / f"{name}.py").read_text()
    assert "def run_script" not in src and "def alert_on_failure" not in src
