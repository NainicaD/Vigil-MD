"""DAG tests for dags/vigil_md_matching.py: structure, defaults, args, the exit-4 skip.

Structure and task callables only. No dag.test(): the tasks call real scripts. Subprocess checks
use --print-only, which makes no GCP call.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "dags"
CONTEXT = {"run_id": "manual__1",
           "params": {"dataset": "vigil_md_dev", "full": False, "workers": 2,
                      "bundle": "b", "baseline": ""}}


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


def test_manual_only_and_the_test_dataset_by_default(dag):
    assert dag.schedule is None or "NullTimetable" in repr(dag.timetable)
    assert dag.max_active_runs == 1
    assert dag.params["dataset"] == "vigil_md_dev" and dag.params["full"] is False


def test_task_order_and_pools(dag):
    t = tasks(dag)
    assert t["candidates"].downstream_task_ids >= {"score"}
    assert t["score"].downstream_task_ids >= {"load_links"}
    assert t["candidates"].pool == "vigil_warehouse" and t["load_links"].pool == "vigil_warehouse"
    assert t["score"].pool == "default_pool"
    for name in ("alert_on_failure", "mark_run_failed"):
        assert t[name].upstream_task_ids == {"candidates", "score", "load_links"}
        assert str(t[name].trigger_rule).lower().endswith("one_failed")


def test_no_other_dag_starts_matching_yet(dag):
    from airflow.models import DagBag
    bag = DagBag(dag_folder=str(DAGS))
    for other in bag.dags.values():
        for task in other.tasks:
            assert getattr(task, "trigger_dag_id", None) != "vigil_md_matching"


def test_args_pass_one_run_folder_from_task_to_task(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "run_script", lambda *a: calls.append(a))
    mod.build_candidates(**CONTEXT)
    mod.score_pairs(**CONTEXT)
    mod.load_links(**CONTEXT)
    cand, score, links = calls
    d = "/opt/airflow/logs/matching/manual__1"
    assert cand[cand.index("--out") + 1] == f"{d}/candidates.parquet" and "--since" in cand
    assert score[score.index("--pairs") + 1] == f"{d}/candidates.parquet"
    assert cand[cand.index("--siblings-out") + 1] == score[score.index("--siblings") + 1] == f"{d}/siblings.csv"
    assert score[score.index("--out") + 1] == links[links.index("--links") + 1] == f"{d}/links.parquet"
    assert "--baseline" not in score and links[links.index("--dataset") + 1] == "vigil_md_dev"


def test_no_candidates_skips_the_rest(mod, monkeypatch):
    from airflow.sdk.exceptions import AirflowSkipException

    def no_pairs(*a):
        raise RuntimeError("s13_build_candidates.py failed (exit 4)\nno candidate pairs")
    monkeypatch.setattr(mod, "run_script", no_pairs)
    with pytest.raises(AirflowSkipException):
        mod.build_candidates(**CONTEXT)


def test_other_candidate_errors_still_fail(mod, monkeypatch):
    def boom(*a):
        raise RuntimeError("s13_build_candidates.py failed (exit 1)")
    monkeypatch.setattr(mod, "run_script", boom)
    with pytest.raises(RuntimeError):
        mod.build_candidates(**CONTEXT)


def test_load_links_args_print_only(mod):
    from vigil_checks import model_links_args
    args = model_links_args("vigil-md-1792696816", "vigil_md_dev", "l.parquet", "r.json", False, "manual__1")
    r = subprocess.run([sys.executable, *args, "--print-only"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0 and "link_maude_gudid_model" in r.stdout, r.stderr[-300:]
