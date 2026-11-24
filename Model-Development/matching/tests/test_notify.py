"""ci/notify.py: the result e-mail of a training run. Offline: no SMTP server is contacted."""
import json

import pytest

from ci import notify

PUBLISHED = "published matching-same-device v1.1.1 ({'mlflow': 'gh-run-1', 'git': 'abc'})\n"
SAME = "not published: same model, encoder, cutoff and IE gate as matching-same-device v1.1.0: skipped\n"
EXISTS = "matching-same-device v1.1.1 is already published: skipped\n"


@pytest.mark.parametrize("gate, publish, requested, log, status", [
    ("failure", "", True, "", "champion kept"),
    ("", "", True, "", "failed"),                         # stopped before the gate
    ("cancelled", "", True, "", "failed"),
    ("success", "", False, "", "not published"),         # by hand without publish
    ("success", "failure", True, "", "failed"),
    ("success", "skipped", True, "", "failed"),
    ("success", "success", True, PUBLISHED, "published"),
    ("success", "success", True, SAME, "no change"),
    ("success", "success", True, EXISTS, "no change"),
    ("success", "success", True, "something else\n", "failed"),
])
def test_outcome(gate, publish, requested, log, status):
    assert notify.outcome(gate, publish, requested, log)[0] == status


def test_a_failed_publish_quotes_the_last_line_of_s11():
    log = "run abc (1 found, newest taken)\ncommit the serving code first, so the git tag points at it:\n M vigil_model/predict.py\n"
    status, detail = notify.outcome("success", "failure", True, log)
    assert status == "failed" and detail.endswith(": M vigil_model/predict.py")
    assert notify.outcome("success", "failure", True, "")[1].endswith("see the run log")


def test_published_names_the_version():
    assert "matching-same-device v1.1.1" in notify.outcome("success", "success", True, PUBLISHED)[1]


def test_failed_checks_come_from_gate_json(tmp_path):
    p = tmp_path / "gate.json"
    p.write_text(json.dumps({"passed": False, "checks": [
        {"name": "precision >= 0.95", "status": "FAIL", "detail": "0.91"},
        {"name": "recall >= 0.8", "status": "PASS", "detail": "0.85"}]}))
    assert notify.failed_checks(p) == ["precision >= 0.95: 0.91"]
    assert notify.failed_checks(tmp_path / "missing.json") == []


def test_message_has_status_reason_checks_and_link():
    subject, html = notify.message("champion kept", "the gate failed", ["precision >= 0.95: 0.91"],
                                   "UDI precision 0.884 < 0.90", "main", "https://github.com/x/runs/1")
    assert subject == "[Vigil-MD] Matching model training: champion kept"
    assert "UDI precision 0.884 &lt; 0.90" in html                     # free text is escaped
    assert "<li>precision &gt;= 0.95: 0.91</li>" in html and 'href="https://github.com/x/runs/1"' in html
    _, html = notify.message("failed", "x", [], "<script>", "main", "u")
    assert "<script>" not in html


def test_without_smtp_settings_it_prints_and_exits_0(tmp_path, monkeypatch, capsys):
    for k in ("SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GATE_OUTCOME", "success")
    monkeypatch.setenv("PUBLISH_OUTCOME", "success")
    monkeypatch.setenv("PUBLISH_REQUESTED", "true")
    (tmp_path / "publish.log").write_text(SAME)
    summary = tmp_path / "summary.md"
    assert notify.main(["--publish-log", str(tmp_path / "publish.log"), "--gate-json", str(tmp_path / "g.json"),
                        "--summary", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "no change:" in out and "e-mail skipped" in out
    assert "**Result: no change**" in summary.read_text()


def test_an_smtp_error_is_reported_not_raised(monkeypatch, capsys):
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("ALERT_EMAIL_TO", "t")

    class Boom:
        def __init__(self, *a, **k):
            raise OSError("no network")
    monkeypatch.setattr(notify.smtplib, "SMTP", Boom)
    assert notify.send("s", "h") is False
    assert capsys.readouterr().out.strip() == "e-mail not sent: OSError"     # no settings in the output
