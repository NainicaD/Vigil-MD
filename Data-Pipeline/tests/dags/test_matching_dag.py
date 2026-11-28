"""DAG tests for dags/vigil_md_matching.py: structure, defaults, args, the exit-4 skip.

Structure and task callables only. No dag.test(): the tasks call real scripts. Subprocess checks
use --print-only, which makes no GCP call.
"""
import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "dags"
CONTEXT = {"run_id": "manual__1",
           "params": {"dataset": "vigil_md_dev", "full": False, "workers": 2,
                      "bundle": "b", "ie_gate": "e6_e3b_c1_m1", "baseline": ""}}


@pytest.fixture(scope="module")
def dag():
    from airflow.models import DagBag
    bag = DagBag(dag_folder=str(DAGS))
    assert bag.import_errors == {}
    return bag.get_dag("vigil_md_matching")


@pytest.fixture
def mod():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("vigil_md_matching_under_test", DAGS / "vigil_md_matching.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def tasks(dag):
    return {t.task_id: t for t in dag.tasks}


def test_no_schedule_prod_by_default_and_the_m1_gate(dag):
    assert dag.schedule is None or "NullTimetable" in repr(dag.timetable)      # vigil_md_load starts it
    assert dag.max_active_runs == 1
    assert dag.params["dataset"] == "vigil_md_prod" and dag.params["full"] is False
    assert dag.params["ie_gate"] == "e6_e3b_c1_m1"


def test_drift_baseline_is_the_committed_reference_by_default(dag):
    from vigil_checks import MATCHING_BASELINE
    assert dag.params["baseline"] == MATCHING_BASELINE
    assert MATCHING_BASELINE.endswith("Model-Development/matching/reports/score_baseline.json")


@pytest.mark.parametrize("exists", [True, False])
def test_score_passes_the_baseline_only_if_the_file_exists(mod, monkeypatch, tmp_path, exists):
    calls = []
    monkeypatch.setattr(mod, "run_script", lambda *a: calls.append(a))
    monkeypatch.setattr(mod, "REPO", tmp_path)
    if exists:
        (tmp_path / "base.json").write_text("{}")
    mod.score_pairs(**{**CONTEXT, "params": {**CONTEXT["params"], "baseline": "base.json"}})
    (score,) = calls
    assert (score[score.index("--baseline") + 1] == "base.json") if exists else ("--baseline" not in score)


def test_task_order_and_pools(dag):
    t = tasks(dag)
    assert t["candidates"].downstream_task_ids >= {"score"}
    assert t["score"].downstream_task_ids >= {"load_links"}
    assert t["load_links"].downstream_task_ids >= {"build_summaries"}          # chatbot tables
    assert t["candidates"].pool == "vigil_warehouse" and t["load_links"].pool == "vigil_warehouse"
    assert t["build_summaries"].pool == "vigil_warehouse" and t["score"].pool == "default_pool"
    for name in ("alert_on_failure", "mark_run_failed"):
        assert t[name].upstream_task_ids == {"candidates", "score", "load_links", "build_summaries",
                                             "check_drift"}
        assert str(t[name].trigger_rule).lower().endswith("one_failed")


def test_the_daily_load_starts_matching_on_prod(dag):
    from airflow.models import DagBag
    bag = DagBag(dag_folder=str(DAGS))
    starters = {(o.dag_id, t.task_id): t for o in bag.dags.values() for t in o.tasks
                if getattr(t, "trigger_dag_id", None) == "vigil_md_matching"}
    assert list(starters) == [("vigil_md_load", "trigger_matching")]
    t = starters[("vigil_md_load", "trigger_matching")]
    assert t.conf == {"dataset": "vigil_md_prod"} and t.wait_for_completion is False
    assert t.skip_when_already_exists is True and t.trigger_run_id == "load__{{ run_id }}"
    assert t.upstream_task_ids == {"build_summaries", "build_links"}
    assert str(t.trigger_rule).lower().endswith("all_done")
    load = tasks(bag.get_dag("vigil_md_load"))
    for name in ("alert_on_failure", "mark_run_failed"):
        assert "trigger_matching" in load[name].upstream_task_ids


def test_args_pass_one_run_folder_from_task_to_task(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "run_script", lambda *a: calls.append(a))
    monkeypatch.setattr(mod, "_model_version", lambda p: "v1.1.0+e6_e3b_c1_m1")
    mod.build_candidates(**CONTEXT)
    mod.score_pairs(**CONTEXT)
    mod.load_links(**CONTEXT)
    mod.build_summaries(**CONTEXT)
    cand, score, links, summ = calls
    assert cand[cand.index("--model-version") + 1] == links[links.index("--model-version") + 1] == \
        "v1.1.0+e6_e3b_c1_m1"
    assert score[score.index("--ie-gate") + 1] == "e6_e3b_c1_m1"
    assert "--full" not in links                                         # no full.flag in this run
    assert summ[summ.index("--dataset") + 1] == "vigil_md_dev" and "--if-changed" in summ
    assert "--model-links" in summ                     # dev is in MODEL_LINK_SUMMARY_DATASETS
    d = "/opt/airflow/logs/matching/manual__1"
    assert cand[cand.index("--out") + 1] == f"{d}/candidates.parquet" and "--since" in cand
    assert score[score.index("--pairs") + 1] == f"{d}/candidates.parquet"
    assert cand[cand.index("--siblings-out") + 1] == score[score.index("--siblings") + 1] == f"{d}/siblings.csv"
    assert score[score.index("--out") + 1] == links[links.index("--links") + 1] == f"{d}/links.parquet"
    assert "--baseline" not in score and links[links.index("--dataset") + 1] == "vigil_md_dev"
    assert score[score.index("--work-dir") + 1] == d                       # checkpoints in the run folder
    assert links[links.index("--chunks") + 1] == f"{d}/chunks.jsonl"


def test_score_retries_twice_and_only_working_tasks_report_to_elk(dag):
    t = tasks(dag)
    assert t["score"].retries == 2
    for name in ("candidates", "score", "load_links", "build_summaries"):
        assert t[name].on_failure_callback and t[name].on_retry_callback
    for name in ("alert_on_failure", "mark_run_failed"):
        assert not t[name].on_failure_callback          # mark_run_failed fails on purpose


def test_task_event_fields_count_the_saved_chunks(tmp_path, monkeypatch):
    import vigil_checks
    import vigil_helpers
    monkeypatch.setattr(vigil_checks, "MATCHING_WORK_DIR", str(tmp_path))
    monkeypatch.setattr(vigil_checks, "matching_files",
                        lambda run_id, work_dir=str(tmp_path): {"chunks": f"{tmp_path}/chunks.jsonl"})
    (tmp_path / "chunks.jsonl").write_text('{"chunk": 0}\n{"chunk": 1}\n')
    ti = types.SimpleNamespace(dag_id="vigil_md_matching", task_id="score", try_number=2)
    f = vigil_helpers.task_event_fields({"ti": ti, "run_id": "manual__1", "exception": ValueError("x" * 900)})
    assert (f["task_id"], f["try_number"], f["chunks_done"]) == ("score", 2, 2)
    assert len(f["error"]) == 500


@pytest.fixture(scope="module")
def check_dag():
    from airflow.models import DagBag
    return DagBag(dag_folder=str(DAGS)).get_dag("vigil_md_matching_check")


@pytest.fixture
def check_mod():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("vigil_md_matching_check_under_test",
                                                  DAGS / "vigil_md_matching_check.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_check_runs_weekly_on_prod_by_default(check_dag):
    assert "0 7 * * 1" in repr(check_dag.timetable) and check_dag.catchup is False
    assert check_dag.is_paused_upon_creation is False            # weekly without a manual unpause
    assert check_dag.params["dataset"] == "vigil_md_prod" and check_dag.params["sample_per_10k"] == 100
    assert check_dag.params["ie_gate"] == "e6_e3b_c1_m1"
    t = tasks(check_dag)
    assert t["candidates"].downstream_task_ids >= {"score"}
    assert t["score"].downstream_task_ids >= {"evaluate"} and t["evaluate"].downstream_task_ids >= {"send_report"}
    assert t["score"].retries == 2 and t["candidates"].pool == "vigil_warehouse"
    assert "load_links" not in t                                       # a check never loads links


def test_check_uses_its_own_folder_and_the_udi_sample(check_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(check_mod, "run_script", lambda *a: calls.append(a))
    ctx = {"run_id": "scheduled__1", "params": {"dataset": "vigil_md_dev", "sample_per_10k": 50, "bundle": "b",
                                                "ie_gate": "e6_e3b_c1_m1"}}
    d = "/opt/airflow/logs/matching/check/scheduled__1"
    monkeypatch.setattr(check_mod.Path, "mkdir", lambda *a, **k: None)
    check_mod.build_candidates(**ctx)
    check_mod.score_pairs(**ctx)
    check_mod.evaluate(**ctx)
    cand, score, ev = calls
    assert cand[cand.index("--mode") + 1] == "udi_check" and cand[cand.index("--sample-per-10k") + 1] == "50"
    assert "--since" not in cand and "--full" not in cand
    assert cand[cand.index("--out") + 1] == f"{d}/candidates.parquet"
    assert score[score.index("--work-dir") + 1] == d
    assert ev[ev.index("--out") + 1] == f"{d}/check_report.json"
    assert score[score.index("--ie-gate") + 1] == ev[ev.index("--ie-gate") + 1] == "e6_e3b_c1_m1"


def test_send_report_sends_one_agreement_record(check_mod, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(check_mod, "send_to_elk", lambda msg, fields: sent.append((msg, fields)))
    monkeypatch.setattr(check_mod, "matching_files",
                        lambda run_id, work_dir: {"check_report": str(tmp_path / "r.json")})
    (tmp_path / "r.json").write_text('{"precision": 0.97, "recall": 0.8}')
    check_mod.send_report(run_id="scheduled__1",
                          params={"dataset": "vigil_md_dev", "sample_per_10k": 100, "bundle": "b",
                                  "ie_gate": ""})
    (msg, fields), = sent
    assert msg == "matching_udi_agreement" and fields["precision"] == 0.97 and fields["run_id"] == "scheduled__1"
    assert fields["n_slice_alert"] == 0 and fields["n_slice_warn"] == 0


def test_send_report_sends_one_record_per_slice(check_mod, monkeypatch, tmp_path):
    import json
    sent = []
    monkeypatch.setattr(check_mod, "send_to_elk", lambda msg, fields: sent.append((msg, fields)))
    monkeypatch.setattr(check_mod, "matching_files",
                        lambda run_id, work_dir: {"check_report": str(tmp_path / "r.json")})
    slices = [{"slice": "matches", "value": "2 or more model groups", "rows": 900, "links": 800,
               "links_right": 552, "precision": 0.69, "precision_ci": [0.657, 0.721],
               "model_recall": 0.7, "level": "alert"},
              {"slice": "event_type", "value": "Death", "rows": 50, "links": 40, "links_right": 39,
               "precision": 0.975, "precision_ci": [0.871, 0.996], "model_recall": None, "level": "small"}]
    (tmp_path / "r.json").write_text(json.dumps({"precision": 0.94, "bundle_version": "v1.1.0",
                                                 "slices": slices, "slice_flags": ["alert: ..."]}))
    check_mod.send_report(run_id="scheduled__2", params={"dataset": "vigil_md_prod", "sample_per_10k": 100,
                                                         "bundle": "b", "ie_gate": ""})
    (m0, overall), (m1, s1), (m2, s2) = sent
    assert m0 == "matching_udi_agreement" and "slices" not in overall          # the list stays in the file
    assert overall["n_slice_alert"] == 1 and overall["slice_flags"] == ["alert: ..."]
    assert m1 == m2 == "matching_udi_slice"
    assert (s1["slice"], s1["slice_level"], s1["precision_ci_low"], s1["bundle_version"]) == (
        "matches", "alert", 0.657, "v1.1.0")
    assert "level" not in s1                    # logger.py's log-level field is left alone
    assert s2["model_recall"] is None and s2["run_id"] == "scheduled__2"


def test_full_flag_from_s13_makes_the_load_replace_every_link(mod, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mod, "run_script", lambda *a: calls.append(a))
    monkeypatch.setattr(mod, "_model_version", lambda p: "v1.1.0+e6_e3b_c1_m1")
    monkeypatch.setattr(mod, "matching_files", lambda run_id: {
        "dir": str(tmp_path), "links": "l", "report": "r", "chunks": "c"})
    (tmp_path / "full.flag").write_text("links were made by another version\n")
    mod.load_links(**CONTEXT)
    assert "--full" in calls[0]


def test_model_version_is_bundle_version_plus_gate():
    from vigil_checks import model_version
    assert model_version({"version": "v1.1.0"}, "e6_e3b_c1_m1") == "v1.1.0+e6_e3b_c1_m1"


def test_callbacks_send_one_record_each(monkeypatch):
    import vigil_helpers
    sent = []
    monkeypatch.setattr(vigil_helpers, "send_to_elk", lambda msg, fields: sent.append(msg))
    monkeypatch.setattr(vigil_helpers, "task_event_fields", lambda ctx: {})
    vigil_helpers.matching_task_failed({})
    vigil_helpers.matching_task_retry({})
    assert sent == ["matching_task_failed", "matching_task_retry"]


def test_no_candidates_skips_the_rest(mod, monkeypatch):
    from airflow.sdk.exceptions import AirflowSkipException

    def no_pairs(*a):
        raise RuntimeError("s13_build_candidates.py failed (exit 4)\nno candidate pairs")
    monkeypatch.setattr(mod, "run_script", no_pairs)
    monkeypatch.setattr(mod, "_model_version", lambda p: "v1.1.0+e6_e3b_c1_m1")   # no bundle here
    with pytest.raises(AirflowSkipException):
        mod.build_candidates(**CONTEXT)


def test_other_candidate_errors_still_fail(mod, monkeypatch):
    def boom(*a):
        raise RuntimeError("s13_build_candidates.py failed (exit 1)")
    monkeypatch.setattr(mod, "run_script", boom)
    monkeypatch.setattr(mod, "_model_version", lambda p: "v1.1.0+e6_e3b_c1_m1")   # no bundle here
    with pytest.raises(RuntimeError):
        mod.build_candidates(**CONTEXT)


def test_load_links_args_print_only(mod):
    from vigil_checks import model_links_args
    args = model_links_args("vigil-md-1792696816", "vigil_md_dev", "l.parquet", "r.json", "c.jsonl",
                            False, "manual__1")
    r = subprocess.run([sys.executable, *args, "--print-only"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "link_maude_gudid_model" in r.stdout, r.stderr[-300:]
