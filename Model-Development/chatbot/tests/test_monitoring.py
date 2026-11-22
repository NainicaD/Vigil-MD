"""Tests for monitoring/: profiles, PSI, the drift check and the baseline (no network, no cost)."""
import json

import pytest

from monitoring import baseline as mb
from monitoring import drift, stats

LIMITS = json.loads((mb.ROOT / "monitoring" / "thresholds.json").read_text())
FP = "fp1"


def rec(labels=("counts",), reply_kind=None, unclear=False, top=0.9, unknown=0.2, words=8):
    return {"labels": list(labels), "reply_kind": reply_kind, "unclear": unclear,
            "top_score": top, "unknown_word_share": unknown, "question_words": words}


def normal(n):
    """A mixed, stable traffic pattern."""
    pattern = [rec(), rec(("recalls",), top=0.8, words=6), rec(("problems",), unknown=0.3),
               rec(("out_of_scope",), reply_kind="medical_refusal", top=0.99, words=10)]
    return [pattern[i % len(pattern)] for i in range(n)]


def event(r, fp=FP):
    return {"event": "router_decision", "rules_fingerprint": fp, **r}


def test_histogram_and_bins():
    h = stats.histogram([0.0, 0.15, 0.97, None], stats.BINS["top_score"])   # None is skipped
    assert h[0] == pytest.approx(2 / 3) and h[-1] == pytest.approx(1 / 3)   # 0.0 and 0.15: bin 0-0.2
    assert stats.histogram([], [0, 1]) == []
    assert stats.histogram([99], stats.BINS["question_words"])[-1] == 1.0   # last bin is open


def test_psi_is_zero_for_the_same_distribution_and_large_for_a_shift():
    a = [0.25, 0.25, 0.25, 0.25]
    assert stats.psi(a, a) == 0.0
    assert stats.psi(a, [0.97, 0.01, 0.01, 0.01]) > 0.25
    assert stats.psi(a, []) is None and stats.psi(a, [1.0]) is None


def test_profile_shares():
    p = stats.profile(normal(400))
    assert p["n"] == 400 and p["refusal_share"] == 0.25 and p["unclear_share"] == 0.0
    assert p["label_share"]["counts"] == 0.25 and p["label_share"]["identity"] == 0.0
    assert stats.profile([])["refusal_share"] is None


def test_same_traffic_is_ok():
    base = stats.profile(normal(400))
    report = stats.compare(base, stats.profile(normal(300)), LIMITS)
    assert report["result"] == "ok"
    assert all(c["level"] == "ok" for c in report["checks"])


def test_new_vocabulary_is_drift():
    base = stats.profile(normal(400))
    now = [dict(r, unknown_word_share=0.8) for r in normal(300)]
    report = stats.compare(base, stats.profile(now), LIMITS)
    assert report["result"] == "drift"
    (c,) = [c for c in report["checks"] if c["name"] == "unknown_word_share"]
    assert c["level"] == "drift" and c["value"] >= LIMITS["psi_drift"]


def test_refusal_share_change_is_drift():
    base = stats.profile(normal(400))
    now = normal(300) + [rec(("out_of_scope",), reply_kind="medical_refusal", top=0.99, words=10)] * 100
    report = stats.compare(base, stats.profile(now), LIMITS)
    (c,) = [c for c in report["checks"] if c["name"] == "refusal_share"]
    assert c["level"] == "drift" and report["result"] == "drift"


def test_too_few_events_give_no_decision_even_if_shifted():
    base = stats.profile(normal(400))
    now = [dict(r, unknown_word_share=0.9) for r in normal(LIMITS["min_events"] - 1)]
    assert stats.compare(base, stats.profile(now), LIMITS)["result"] == "insufficient_data"


def test_check_compares_only_the_baselines_router():
    base = {"rules_fingerprint": FP, "profile": stats.profile(normal(400))}
    events = [event(r) for r in normal(250)] + [event(rec(unknown=0.9), fp="other")] * 500
    report = drift.check(events, base, LIMITS)
    assert report["result"] == "ok" and report["n"] == 250
    assert report["events_other_router"] == 500
    only_other = drift.check([event(rec(), fp="other")] * 300, base, LIMITS)
    assert only_other["result"] == "insufficient_data" and "rebuild the baseline" in only_other["note"]


def test_read_events_accepts_plain_and_cloud_logging_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join([
        json.dumps(event(rec())),
        json.dumps({"jsonPayload": event(rec())}),
        json.dumps({"textPayload": "uvicorn started"}),
        json.dumps({"event": "something_else"}), ""]))
    assert len(drift.read_events(path)) == 2


@pytest.mark.parametrize("unknown, code", [(None, drift.EXIT_OK), (0.9, drift.EXIT_DRIFT)])
def test_drift_cli_exit_codes(tmp_path, capsys, unknown, code):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"rules_fingerprint": FP, "profile": stats.profile(normal(400))}))
    ev = tmp_path / "events.jsonl"
    ev.write_text("\n".join(json.dumps(event(r if unknown is None else dict(r, unknown_word_share=unknown)))
                            for r in normal(300)))
    out = tmp_path / "report.json"
    assert drift.main(["--events", str(ev), "--baseline", str(base), "--out", str(out)]) == code
    assert json.loads(out.read_text())["n"] == 300
    assert "result:" in capsys.readouterr().out


def test_drift_cli_no_decision(tmp_path):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"rules_fingerprint": FP, "profile": stats.profile(normal(400))}))
    ev = tmp_path / "events.jsonl"
    ev.write_text(json.dumps(event(rec())))
    assert drift.main(["--events", str(ev), "--baseline", str(base)]) == drift.EXIT_NO_DECISION


def test_baseline_from_events_uses_the_most_common_router():
    events = [dict(event(r), model_version="v1") for r in normal(300)] + [event(rec(), fp="old")] * 10
    base = mb.from_events(events, "ref.jsonl")
    assert base["rules_fingerprint"] == FP and base["model_version"] == "v1"
    assert base["profile"]["n"] == 300 and base["data"] == "events:ref.jsonl"


def test_events_from_the_api_fit_the_monitoring_records():
    from api import events
    out = {"status": "answered", "labels": ["counts"], "reply_kind": None,
           "router_signals": {"scores": {"counts": 0.9}, "unclear": False,
                              "unknown_word_share": 0.1}}
    e = events.build_event(out, "How many deaths?", latency_ms=1, model_version=None,
                           dataset="d", entity_given=False)
    assert stats.record_from_event(e) == rec(top=0.9, unknown=0.1, words=3)


def test_shipped_baseline_is_complete():
    base = json.loads(mb.DEFAULT_OUT.read_text())
    assert base["rules_fingerprint"] and base["profile"]["n"] > 0
    assert set(base["profile"]["hist"]) == set(stats.BINS)
    assert all(len(h) == len(stats.BINS[k]) for k, h in base["profile"]["hist"].items())


def test_baseline_records_from_a_trained_router(tmp_path):
    from query_router import generate as gen
    from query_router.predict import QueryRouter
    from query_router.train import save, train
    data = gen.generate(n_train=300, n_val=100, seed=3)
    pipe, meta = train(data["train"], data["val"], c_grid=(1.0,))
    save(pipe, meta, tmp_path)
    router = QueryRouter.load(tmp_path)
    r = mb.record(router, "How many deaths has the DreamStation had?")
    assert set(r) == set(stats.RECORD_KEYS) and 0 <= r["top_score"] <= 1
    assert r["question_words"] == 7 and r["unknown_word_share"] is not None


# ── BigQuery source (fake client) ────────────────────────────────────────────

class FakeBQ:
    def __init__(self, rows=(), est=1_000_000, missing=False):
        self.rows, self.est, self.missing, self.calls = list(rows), est, missing, []

    def query(self, sql, job_config):
        import types
        self.calls.append((sql, job_config))
        if self.missing:
            from google.api_core import exceptions
            raise exceptions.NotFound("table")
        if job_config.dry_run:
            return types.SimpleNamespace(total_bytes_processed=self.est)
        return types.SimpleNamespace(result=lambda: iter(self.rows), total_bytes_billed=10_485_760)


def test_read_window_dry_runs_caps_and_returns_events():
    from monitoring import bq_events
    rows = [{"event": event(rec())}, {"event": None}]
    client = FakeBQ(rows)
    events, info = bq_events.read_window(7, client=client)
    assert len(events) == 1 and events[0]["rules_fingerprint"] == FP
    dry, real = client.calls
    assert dry[1].dry_run and not real[1].dry_run
    assert real[1].maximum_bytes_billed == int(bq_events.DEFAULT_MAX_GB * 1e9)
    assert "INTERVAL @days DAY" in real[0] and "router_decision" in real[0]
    assert info["billed_gb"] == 0.010486 and info["days"] == 7


def test_read_window_refuses_above_the_cap_and_reports_a_missing_table():
    from monitoring import bq_events
    client = FakeBQ(est=int(0.5e9))
    with pytest.raises(bq_events.BudgetExceeded):
        bq_events.read_window(7, client=client, max_gb=0.1)
    assert len(client.calls) == 1                         # only the dry run
    with pytest.raises(bq_events.NoEventsTable):
        bq_events.read_window(7, client=FakeBQ(missing=True))


def test_drift_cli_from_bigquery(tmp_path, monkeypatch):
    from monitoring import bq_events
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"rules_fingerprint": FP, "profile": stats.profile(normal(400))}))
    shifted = [event(dict(r, unknown_word_share=0.9)) for r in normal(300)]
    monkeypatch.setattr(bq_events, "read_window", lambda days, max_gb: (
        shifted, {"table": "t", "days": days, "billed_gb": 0.01}))
    summary = tmp_path / "summary.md"
    code = drift.main(["--from-bigquery", "--days", "3", "--baseline", str(base),
                       "--summary", str(summary)])
    assert code == drift.EXIT_DRIFT
    text = summary.read_text()
    assert "DRIFT" in text and "unknown_word_share" in text and "last 3 days" in text


def test_drift_cli_from_bigquery_without_a_table_is_no_decision(tmp_path, monkeypatch):
    from monitoring import bq_events

    def missing(days, max_gb):
        raise bq_events.NoEventsTable("does not exist yet")
    monkeypatch.setattr(bq_events, "read_window", missing)
    summary = tmp_path / "s.md"
    assert drift.main(["--from-bigquery", "--summary", str(summary)]) == drift.EXIT_NO_DECISION
    assert "NO DECISION" in summary.read_text() and "does not exist yet" in summary.read_text()


def test_drift_cli_over_the_cap_is_an_error(monkeypatch):
    from monitoring import bq_events

    def over(days, max_gb):
        raise bq_events.BudgetExceeded("too big")
    monkeypatch.setattr(bq_events, "read_window", over)
    assert drift.main(["--from-bigquery"]) == 1


def test_monitoring_workflow():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((mb.ROOT.parents[1] / ".github" / "workflows" / "router-monitoring.yml")
                        .read_text())
    on = wf[True]                                       # YAML reads the key "on" as True
    assert on["schedule"][0]["cron"] and "workflow_dispatch" in on
    job = wf["jobs"]["drift"]
    assert job["permissions"]["id-token"] == "write"
    run = next(s["run"] for s in job["steps"] if s.get("id") == "drift")
    assert "--from-bigquery" in run and "--max-gb 0.1" in run
    assert "3) " in run and "exit 1" in run              # drift fails the run (GitHub e-mails)
    assert "vars.GCP_MONITOR_SA" in (mb.ROOT.parents[1] / ".github" / "workflows" /
                                     "router-monitoring.yml").read_text()


def test_drift_writes_the_retraining_issue_only_on_drift(tmp_path):
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"rules_fingerprint": FP, "profile": stats.profile(normal(400))}))
    for unknown, expect_issue in ((None, False), (0.9, True)):
        ev = tmp_path / "events.jsonl"
        ev.write_text("\n".join(json.dumps(event(r if unknown is None else
                                                 dict(r, unknown_word_share=unknown)))
                                for r in normal(300)))
        issue = tmp_path / "issue.md"
        issue.unlink(missing_ok=True)
        drift.main(["--events", str(ev), "--baseline", str(base), "--issue-body", str(issue),
                    "--run-url", "https://example.test/run/1"])
        assert issue.exists() is expect_issue
    text = issue.read_text()
    assert "DRIFT" in text and "unknown_word_share" in text and "https://example.test/run/1" in text
    assert "- [ ] Open a pull request" in text and "monitoring.baseline" in text


def test_monitoring_workflow_opens_one_issue_on_drift():
    yaml = pytest.importorskip("yaml")
    path = mb.ROOT.parents[1] / ".github" / "workflows" / "router-monitoring.yml"
    job = yaml.safe_load(path.read_text())["jobs"]["drift"]
    assert job["permissions"]["issues"] == "write"
    step = next(s for s in job["steps"] if s.get("name", "").startswith("Open the retraining issue"))
    assert "steps.drift.outputs.code == '3'" in step["if"]
    assert "gh issue list --state open --label router-drift" in step["run"]
    assert "gh issue comment" in step["run"] and "gh issue create" in step["run"]
