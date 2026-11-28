"""Tests for the retraining trigger (Model Deployment guidelines 5.3): the rules in vigil_checks,
the GitHub / e-mail / ELK steps in vigil_helpers, and the tasks decide_retrain (weekly check) and
check_drift (matching DAG). No network: GitHub, SMTP and ELK are replaced by fakes.
"""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "dags"
sys.path.insert(0, str(DAGS))

import vigil_checks as C  # noqa: E402
import vigil_helpers as H  # noqa: E402

NOW = datetime(2026, 11, 30, 7, 30, tzinfo=timezone.utc)


# ── rules (pure) ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("precision, psi, expected", [
    (0.941, None, None),                      # 24 Nov prod: the warning band does not retrain
    (0.90, None, None),                       # at the level: no
    (0.884, None, "UDI precision 0.884 < 0.90"),
    (None, 0.2, None),
    (None, 0.25, "score drift (PSI) 0.250 > 0.20"),
    (None, None, None),                       # no baseline, or s12 skipped the PSI
    (0.80, 0.30, "UDI precision 0.800 < 0.90; score drift (PSI) 0.300 > 0.20"),
])
def test_retrain_reason(precision, psi, expected):
    assert C.retrain_reason(precision=precision, psi=psi) == expected


def test_retrain_reason_with_a_demo_floor():
    assert C.retrain_reason(precision=0.941, precision_floor=0.99) == "UDI precision 0.941 < 0.99"
    # the dev test of 24 Nov showed "0.992 < 1.00" for a floor of 0.999
    assert C.retrain_reason(precision=0.992224, precision_floor=0.999) == "UDI precision 0.992 < 0.999"


def test_recent_run_is_the_newest_within_the_cooldown():
    runs = [{"created_at": "2026-11-20T07:00:00Z", "id": 1},     # 10 days ago: outside
            {"created_at": "2026-11-25T07:00:00Z", "id": 2},
            {"created_at": "2026-11-28T07:00:00Z", "id": 3}]
    assert C.recent_run(runs, NOW)["id"] == 3
    assert C.recent_run(runs[:1], NOW) is None and C.recent_run([], NOW) is None
    assert C.recent_run([{"created_at": "2026-11-23T07:31:00Z"}], NOW) is not None   # 6 d 23 h 59 min
    assert C.recent_run([{"created_at": "2026-11-23T07:30:00Z"}], NOW) is None       # exactly 7 days


def test_dispatch_body_publishes_and_caps_the_reason():
    body = C.dispatch_body("main", "x" * 500)
    assert body["ref"] == "main" and body["inputs"]["publish"] == "true"
    assert len(body["inputs"]["reason"]) == 200
    assert body["inputs"]["package"] == "matching-same-device" and body["inputs"]["semver"] == "auto"


def test_dispatch_body_for_a_test_package():
    body = C.dispatch_body("monitoring/retrain-trigger", "r", "matching-same-device-test", "0.0.1")
    assert body["inputs"] == {"reason": "r", "publish": "true", "package": "matching-same-device-test",
                              "semver": "0.0.1"}


def test_dispatch_inputs_exist_in_the_workflow():
    """GitHub refuses (HTTP 422) a dispatch with an input the workflow does not declare."""
    import yaml
    wf = yaml.safe_load((ROOT.parent / ".github" / "workflows" / "model-training.yml").read_text())
    declared = (wf.get("on") or wf[True])["workflow_dispatch"]["inputs"]
    assert set(C.dispatch_body("main", "r")["inputs"]) <= set(declared)


def test_trigger_levels_match_the_alerts():
    sys.path.insert(0, str(ROOT.parent / "elk" / "kibana"))
    import setup_kibana as K
    assert "precision < 0.90" in K.RULES["vigil-md-udi-precision-alert"][1]
    assert "score_psi > 0.2" in K.RULES["vigil-md-score-drift"][1]
    assert C.RETRAIN_PRECISION == 0.90 and C.RETRAIN_PSI == 0.2
    # per slice: s14 sets level from the same 0.95 / 0.90, Kibana fires on it
    assert "slice_level:alert" in K.RULES["vigil-md-udi-slice-alert"][1]
    assert "slice_level:warn" in K.RULES["vigil-md-udi-slice-warn"][1]
    s14 = (ROOT.parent / "Model-Development" / "matching" / "scripts" / "s14_udi_agreement.py").read_text()
    assert "WARN, ALERT = 0.95, 0.90" in s14


# ── GitHub calls ─────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body


def test_github_errors_name_the_status_but_never_the_token(monkeypatch):
    calls = []
    monkeypatch.setattr(H, "_github", lambda m, p, token, **kw: calls.append((m, p, kw)) or
                        FakeResponse(422, text='{"message":"Unexpected inputs provided: [\\"publish\\"]"}'))
    with pytest.raises(RuntimeError) as e:
        H.start_workflow("secret-token", "o/r", "w.yml", {"ref": "main", "inputs": {}})
    assert "HTTP 422" in str(e.value) and "Unexpected inputs" in str(e.value)
    assert "secret-token" not in str(e.value)
    assert calls[0][:2] == ("POST", "/repos/o/r/actions/workflows/w.yml/dispatches")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        monkeypatch.setattr(H, "_github", lambda *a, **k: FakeResponse(401, text="Bad credentials"))
        H.dispatched_runs("secret-token", "o/r", "w.yml", NOW)


def test_dispatched_runs_asks_for_manual_runs_since_the_date(monkeypatch):
    seen = {}
    def fake(method, path, token, **kw):
        seen.update(method=method, path=path, **kw)
        return FakeResponse(200, {"workflow_runs": [{"id": 7}]})
    monkeypatch.setattr(H, "_github", fake)
    assert H.dispatched_runs("t", "o/r", "w.yml", NOW) == [{"id": 7}]
    assert seen["method"] == "GET" and seen["params"]["event"] == "workflow_dispatch"
    assert seen["params"]["created"] == ">=2026-11-30"


def test_github_request_sends_the_token_in_the_header_only(monkeypatch):
    import requests
    seen = {}
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: seen.update(url=url, **kw) or "ok")
    assert H._github("GET", "/x", "tok") == "ok"
    assert seen["url"] == "https://api.github.com/x" and "tok" not in seen["url"]
    assert seen["headers"]["Authorization"] == "Bearer tok" and seen["timeout"] == H.GITHUB_TIMEOUT_S


# ── retrain_if_needed ────────────────────────────────────────────────────────

class TI:
    dag_id = "vigil_md_matching_check"


@pytest.fixture
def fakes(monkeypatch):
    log = {"elk": [], "mail": [], "started": [], "runs": []}
    monkeypatch.setenv("VIGIL_GITHUB_TOKEN", "tok")
    monkeypatch.setattr(H, "send_to_elk", lambda m, f: log["elk"].append((m, f)))
    monkeypatch.setattr(H, "send_email", lambda s, h: log["mail"].append((s, h)) or True)
    monkeypatch.setattr(H, "start_workflow", lambda token, repo, wf, body: log["started"].append((repo, wf, body)))
    monkeypatch.setattr(H, "dispatched_runs", lambda token, repo, wf, since: log["runs"])
    return log


CTX = {"ti": TI(), "run_id": "scheduled__2026-11-30"}


def test_no_reason_does_nothing_and_needs_no_token(fakes, monkeypatch):
    monkeypatch.delenv("VIGIL_GITHUB_TOKEN")
    assert H.retrain_if_needed(None, CTX, "main", {"precision": 0.941}) == "not_needed"
    assert fakes["elk"] == fakes["mail"] == fakes["started"] == []


def test_a_reason_starts_the_workflow_mails_and_records(fakes):
    assert H.retrain_if_needed("UDI precision 0.884 < 0.90", CTX, "main", {"precision": 0.884}) == "started"
    (repo, wf, body), = fakes["started"]
    assert (repo, wf) == ("NainicaD/Vigil-MD", "model-training.yml")
    assert body == {"ref": "main", "inputs": {"reason": "UDI precision 0.884 < 0.90", "publish": "true",
                                              "package": "matching-same-device", "semver": "auto"}}
    (msg, fields), = fakes["elk"]
    assert msg == "matching_retrain_triggered" and fields["outcome"] == "started"
    assert fields["package"] == "matching-same-device"
    assert fields["precision"] == 0.884 and fields["run_id"] == "scheduled__2026-11-30"
    assert "tok" not in json.dumps(fields)
    (subject, html), = fakes["mail"]
    assert subject == "[Vigil-MD] Retraining started: UDI precision 0.884 < 0.90" and "model gate" in html


def test_a_test_package_reaches_the_workflow_and_the_email(fakes):
    H.retrain_if_needed("r", CTX, "monitoring/retrain-trigger", {}, "matching-same-device-test", "0.0.1")
    (_, _, body), = fakes["started"]
    assert body["inputs"]["package"] == "matching-same-device-test" and body["inputs"]["semver"] == "0.0.1"
    (_, html), = fakes["mail"]
    assert "matching-same-device-test" in html


def test_a_run_in_the_last_7_days_holds_it_back(fakes):
    fakes["runs"].append({"created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "html_url": "https://github.com/x/runs/1"})
    assert H.retrain_if_needed("UDI precision 0.884 < 0.90", CTX, "main", {}) == "cooldown"
    assert fakes["started"] == [] and fakes["mail"] == []
    (msg, fields), = fakes["elk"]
    assert fields["outcome"] == "cooldown" and fields["last_run_url"] == "https://github.com/x/runs/1"


def test_a_needed_retrain_without_a_token_fails_the_task(fakes, monkeypatch):
    from airflow.sdk.exceptions import AirflowFailException
    monkeypatch.delenv("VIGIL_GITHUB_TOKEN")
    with pytest.raises(AirflowFailException, match="VIGIL_GITHUB_TOKEN is not set"):
        H.retrain_if_needed("UDI precision 0.884 < 0.90", CTX, "main", {})
    assert fakes["started"] == []


def test_send_email_skips_without_settings(monkeypatch, caplog):
    monkeypatch.delenv("VIGIL_ALERT_EMAIL_TO", raising=False)
    assert H.send_email("s", "<p>h</p>") is False
    assert "VIGIL_ALERT_EMAIL_TO not set" in caplog.text


# ── the tasks ────────────────────────────────────────────────────────────────

def load(name):
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(f"{name}_retrain_test", DAGS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def capture(mod, monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "retrain_if_needed",
                        lambda reason, ctx, ref, metrics, package, semver:
                        seen.append((reason, ref, metrics, package, semver)) or "x")
    return seen


@pytest.mark.parametrize("floor, reason", [(0.90, None), (0.99, "UDI precision 0.941 < 0.99")])
def test_decide_retrain_reads_the_check_report(monkeypatch, tmp_path, floor, reason):
    mod = load("vigil_md_matching_check")
    seen = capture(mod, monkeypatch)
    monkeypatch.setattr(mod, "matching_files", lambda run_id, work_dir: {"check_report": str(tmp_path / "r.json")})
    (tmp_path / "r.json").write_text('{"precision": 0.941206, "recall": 0.700654}')
    mod.decide_retrain(ti=TI(), run_id="r", params={
        "dataset": "vigil_md_dev", "sample_per_10k": 100, "bundle": "b", "ie_gate": "",
        "retrain_precision": floor, "retrain_ref": "monitoring/retrain-trigger",
        "retrain_package": "matching-same-device-test", "retrain_semver": "0.0.1"})
    (got, ref, metrics, package, semver), = seen
    assert got == reason and ref == "monitoring/retrain-trigger"
    assert (package, semver) == ("matching-same-device-test", "0.0.1")
    assert metrics == {"precision": 0.941206, "precision_floor": floor, "dataset": "vigil_md_dev"}


@pytest.mark.parametrize("report, reason", [
    ({"n_pairs": 50_000, "score_psi": 0.31}, "score drift (PSI) 0.310 > 0.20"),
    ({"n_pairs": 50_000, "score_psi": 0.01}, None),
    ({"n_pairs": 300, "score_psi": None, "score_psi_skipped": "300 pairs < 1000"}, None),
    ({"n_pairs": 50_000}, None),                                   # no baseline file
])
def test_check_drift_reads_the_run_report(monkeypatch, tmp_path, report, reason):
    mod = load("vigil_md_matching")
    seen = capture(mod, monkeypatch)
    monkeypatch.setattr(mod, "matching_files", lambda run_id: {"report": str(tmp_path / "report.json")})
    (tmp_path / "report.json").write_text(json.dumps(report))
    mod.check_drift(ti=TI(), run_id="r", params={
        "dataset": "vigil_md_prod", "full": False, "workers": 1, "bundle": "b", "ie_gate": "",
        "baseline": "", "retrain_ref": "main", "retrain_package": "matching-same-device",
        "retrain_semver": "auto"})
    (got, ref, metrics, package, semver), = seen
    assert got == reason and ref == "main" and metrics["n_pairs"] == report["n_pairs"]
    assert (package, semver) == ("matching-same-device", "auto")


def test_the_trigger_tasks_have_no_retry_and_their_place_in_the_dags():
    from airflow.models import DagBag
    bag = DagBag(dag_folder=str(DAGS))
    check, matching = bag.get_dag("vigil_md_matching_check"), bag.get_dag("vigil_md_matching")
    decide, drift = check.get_task("decide_retrain"), matching.get_task("check_drift")
    assert decide.retries == 0 and drift.retries == 0                # a retry could start it twice
    assert decide.upstream_task_ids == {"send_report"}
    assert drift.upstream_task_ids == {"score"} and drift.downstream_task_ids >= {"alert_on_failure"}
    assert "check_drift" not in matching.get_task("load_links").upstream_task_ids   # never holds up the load
    assert check.params["retrain_precision"] == 0.90 and check.params["retrain_ref"] == "main"
    for dag in (check, matching):
        assert dag.params["retrain_ref"] == "main" and dag.params["retrain_semver"] == "auto"
        assert dag.params["retrain_package"] == "matching-same-device"           # the real package by default
    for dag, name in ((check, "decide_retrain"), (matching, "check_drift")):
        assert name in dag.get_task("alert_on_failure").upstream_task_ids
        assert name in dag.get_task("mark_run_failed").upstream_task_ids
