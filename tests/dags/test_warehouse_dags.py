"""DAG tests for dags/vigil_md_acquisition.py (trigger_load wiring),
dags/vigil_md_load.py and dags/vigil_md_full_load.py.

Structure and task callables only. No dag.test(): the tasks call real scripts.
HTTP and GCS are faked (fake_gcp + FakeBucket). Subprocess checks use
--print-only, which makes no GCP call.
"""
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.conftest import FakeBucket

ROOT = Path(__file__).resolve().parents[2]
DAGS = ROOT / "dags"
SOURCES = ["enforcement", "recall", "maude", "gudid"]
STEPS = ["unzip", "load_raw", "staging", "merge"]
LOAD_DAG_STEPS = ["unzip", "load_raw", "staging", "validate", "check", "merge"]


@pytest.fixture(scope="module")
def dagbag():
    from airflow.models import DagBag
    return DagBag(dag_folder=str(DAGS))


def load_module(name):
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", DAGS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def acq():
    return load_module("vigil_md_acquisition")


@pytest.fixture
def full():
    return load_module("vigil_md_full_load")


@pytest.fixture
def load():
    return load_module("vigil_md_load")


def tasks(dag):
    return {t.task_id: t for t in dag.tasks}


def test_all_warehouse_dags_parse(dagbag):
    assert dagbag.import_errors == {}
    assert {"vigil_md_acquisition", "vigil_md_load", "vigil_md_full_load"} <= set(dagbag.dag_ids)


# ── vigil_md_acquisition ─────────────────────────────────────────────────────

def test_acquisition_schedule_is_unchanged(dagbag):
    dag = dagbag.get_dag("vigil_md_acquisition")
    assert "expression='0 6 * * *'" in repr(dag.timetable)
    assert dag.catchup is False and dag.max_active_runs == 1


def test_acquisition_branch_targets(dagbag):
    t = tasks(dagbag.get_dag("vigil_md_acquisition"))
    assert t["check_manifest"].downstream_task_ids == (
        {f"fetch.fetch_{s}" for s in SOURCES} | {"no_update", "trigger_load"})
    assert t["trigger_load"].upstream_task_ids == {"check_manifest", "log_summary"}


def test_acquisition_trigger_rules(dagbag):
    from airflow.task.trigger_rule import TriggerRule as TR
    t = tasks(dagbag.get_dag("vigil_md_acquisition"))
    assert t["validate_gcs"].trigger_rule == TR.NONE_FAILED_MIN_ONE_SUCCESS
    assert t["trigger_load"].trigger_rule == TR.NONE_FAILED_MIN_ONE_SUCCESS
    assert t["log_summary"].trigger_rule == TR.ALL_SUCCESS
    assert t["alert_on_failure"].trigger_rule == TR.ONE_FAILED
    assert "alert_on_failure" in t["trigger_load"].downstream_task_ids


def test_acquisition_has_no_warehouse_tasks(dagbag):
    t = tasks(dagbag.get_dag("vigil_md_acquisition"))
    assert not any(tid.startswith(("warehouse", "load.")) for tid in t)


def test_trigger_load_starts_the_loading_dag_once(dagbag):
    tl = tasks(dagbag.get_dag("vigil_md_acquisition"))["trigger_load"]
    assert tl.trigger_dag_id == "vigil_md_load"
    assert tl.conf == {"ingest_date": "{{ ds }}"}
    assert tl.trigger_run_id == "acq__{{ run_id }}"
    assert tl.skip_when_already_exists is True     # a retried acquisition run does not load twice
    assert tl.wait_for_completion is False


def test_fetch_one_source_uses_incremental_mode(acq, monkeypatch):
    calls = []
    monkeypatch.setattr(acq, "run_script", lambda *a: calls.append(a))
    acq.fetch_one_source("maude")
    (args,) = calls
    assert args[args.index("--mode") + 1] == "incremental"


# ── check_manifest branch ────────────────────────────────────────────────────

STATE = {"openfda_enforcement_export_date": "E1", "openfda_recall_export_date": "R1",
         "openfda_event_export_date": "M1", "gudid_snapshot": "20261001"}
SAME = {"enforcement": "E1", "recall": "R1", "event": "M1"}


@pytest.fixture
def branch(acq, fake_gcp, monkeypatch):
    bucket = FakeBucket(acq.BUCKET)
    bucket.store["manifests/manifest_state.json"] = json.dumps(STATE).encode()
    client = fake_gcp.storage.Client.return_value
    client.bucket.return_value = bucket
    client.list_blobs.side_effect = bucket.client.list_blobs

    def run(dates, snapshot, pending=False):
        if pending:
            bucket.store["manifests/pending/maude/20261024T060000Z.json"] = b"[]"
        link = f"https://accessgudid.nlm.nih.gov/x/gudid_full_release_{snapshot}.zip"
        manifest = {"results": {"device": {e: {"export_date": d} for e, d in dates.items()}}}

        def get(url, timeout=None):
            if "download.json" in url:
                return types.SimpleNamespace(json=lambda: manifest)
            return types.SimpleNamespace(
                content=f"<rss><channel><item><link>{link}</link></item></channel></rss>".encode())
        monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=get))
        return acq.check_manifest()
    return run


def test_branch_fetches_only_changed_sources(branch):
    assert branch(dict(SAME, event="M2"), "20261001") == ["fetch.fetch_maude", "trigger_load"]


def test_branch_includes_gudid_on_new_snapshot(branch):
    assert branch(dict(SAME, recall="R2"), "20261101") == [
        "fetch.fetch_recall", "fetch.fetch_gudid", "trigger_load"]


def test_branch_triggers_load_for_pending_files(branch):
    assert branch(SAME, "20261001", pending=True) == "trigger_load"


def test_branch_no_update(branch):
    assert branch(SAME, "20261001") == "no_update"


# ── script arguments (accepted by each script's argparse) ────────────────────

def run_print_only(args):
    return subprocess.run([sys.executable, *args, "--print-only"], cwd=ROOT,
                          capture_output=True, text=True)


@pytest.mark.parametrize("step", STEPS)
def test_warehouse_args_parse(load, step):
    args = load.warehouse_args(step, "maude", "2026-10-25")
    r = run_print_only(args)
    assert r.returncode == 0, r.stderr[-500:]
    if step in ("unzip", "load_raw"):
        assert args[args.index("--scope") + 1] == "changed"


@pytest.mark.parametrize("step, rebuild", [(s, False) for s in STEPS] + [("merge", True)])
def test_full_load_args_parse(full, step, rebuild):
    args = full.full_load_args(step, "maude", "2026-10-25", rebuild)
    r = run_print_only(args)
    assert r.returncode == 0, r.stderr[-500:]
    if rebuild:
        assert "REBUILD script" in r.stdout


def test_full_load_args_modes(full):
    for step in ("unzip", "load_raw"):
        args = full.full_load_args(step, "x", "D")
        assert args[args.index("--scope") + 1] == "all"
    assert full.full_load_args("load_raw", "x", "D")[-2:] == ["--mode", "full"]
    assert "--rebuild" not in full.full_load_args("merge", "x", "D")


# ── vigil_md_load ────────────────────────────────────────────────────────────

def test_load_dag_is_triggered_only(dagbag):
    dag = dagbag.get_dag("vigil_md_load")
    assert type(dag.timetable).__name__ == "NullTimetable"
    assert dag.catchup is False and dag.max_active_runs == 1
    assert "ingest_date" in dag.params


@pytest.mark.parametrize("source", SOURCES)
def test_load_dag_chain(dagbag, source):
    t = tasks(dagbag.get_dag("vigil_md_load"))
    chain = [f"load.{step}_{source}" for step in LOAD_DAG_STEPS]
    assert t[chain[0]].upstream_task_ids == set()          # chains start the run
    for up, down in zip(chain, chain[1:]):
        assert t[down].upstream_task_ids == {up}
    assert all(t[c].pool == "vigil_warehouse" for c in chain)
    # TaskGroup >> alert links the chain's last task; ONE_FAILED also counts upstream_failed
    assert "alert_on_failure" in t[chain[-1]].downstream_task_ids


@pytest.mark.parametrize("context, expected", [
    ({"dag_run": types.SimpleNamespace(conf={"ingest_date": "2026-10-25"}), "ds": "2026-10-30"},
     "2026-10-25"),                                                    # trigger_load conf wins
    ({"params": {"ingest_date": "2026-10-24"}, "ds": "2026-10-30"}, "2026-10-24"),   # manual param
    ({"params": {"ingest_date": None}, "ds": "2026-10-30"}, "2026-10-30"),           # run's date
])
def test_resolve_ingest_date(load, context, expected):
    assert load.resolve_ingest_date(context) == expected


@pytest.mark.parametrize("context", [{}, {"params": {"ingest_date": "25/10/2026"}}])
def test_resolve_ingest_date_rejects_missing_or_bad(load, context):
    with pytest.raises(ValueError):
        load.resolve_ingest_date(context)


def test_load_step_uses_the_triggered_ingest_date(load, monkeypatch):
    calls = []
    monkeypatch.setattr(load, "run_script", lambda *a: calls.append(a))
    load.run_load_step("staging", "maude",
                       dag_run=types.SimpleNamespace(conf={"ingest_date": "2026-10-25"}), ds="2026-10-30")
    (args,) = calls
    assert args[args.index("--ingest-date") + 1] == "2026-10-25"


# ── vigil_md_full_load ───────────────────────────────────────────────────────

def test_full_load_is_manual_with_params(dagbag):
    dag = dagbag.get_dag("vigil_md_full_load")
    assert type(dag.timetable).__name__ == "NullTimetable"
    assert dag.catchup is False and dag.max_active_runs == 1
    assert dag.params["sources"] == SOURCES
    assert dag.params["refetch"] is False and dag.params["rebuild_prod"] is False


@pytest.mark.parametrize("source", SOURCES)
def test_full_load_chain(dagbag, source):
    t = tasks(dagbag.get_dag("vigil_md_full_load"))
    assert t["guard_no_acquisition_run"].downstream_task_ids == {"refetch", "alert_on_failure"}
    chain = [f"load.{step}_{source}" for step in LOAD_DAG_STEPS]
    assert t[chain[0]].upstream_task_ids == {"refetch"}
    for up, down in zip(chain, chain[1:]):
        assert t[down].upstream_task_ids == {up}
    assert all(t[c].pool == "vigil_warehouse" for c in chain + ["refetch"])
    assert "alert_on_failure" in t[chain[-1]].downstream_task_ids


@pytest.mark.parametrize("params", [{"sources": []}, {"sources": ["maude", "nope"]}, {}])
def test_selected_sources_rejects_bad_params(full, params):
    with pytest.raises(ValueError):
        full.selected_sources(params)


class FakeTI:
    """running: {dag_id: number of runs in progress}."""
    def __init__(self, running=None):
        self.running, self.calls = running or {}, []

    def get_dr_count(self, dag_id, states=None):
        self.calls.append((dag_id, states))
        return self.running.get(dag_id, 0)


def test_guard_passes_when_no_daily_run(full):
    ti = FakeTI()
    full.guard_no_acquisition_run(ti=ti, params={"sources": ["maude"]})
    assert ti.calls == [("vigil_md_acquisition", ["running", "queued"]),
                        ("vigil_md_load", ["running", "queued"])]


@pytest.mark.parametrize("dag_id", ["vigil_md_acquisition", "vigil_md_load"])
def test_guard_fails_when_a_daily_dag_runs(full, dag_id):
    with pytest.raises(RuntimeError, match=f"{dag_id} has 1 run"):
        full.guard_no_acquisition_run(ti=FakeTI({dag_id: 1}), params={"sources": ["maude"]})


@pytest.fixture
def recorded(full, monkeypatch):
    calls = []
    monkeypatch.setattr(full, "run_script", lambda *a: calls.append(a))
    return calls


def test_refetch_false_fetches_nothing(full, recorded):
    full.refetch(params={"refetch": False, "sources": ["maude"]})
    assert recorded == []


def test_refetch_true_uses_full_mode_per_source(full, recorded):
    full.refetch(params={"refetch": True, "sources": ["maude", "gudid"]})
    assert [c[c.index("--source") + 1] for c in recorded] == ["maude", "gudid"]
    assert all(c[c.index("--mode") + 1] == "full" for c in recorded)


def test_unselected_source_is_skipped(full, recorded):
    from airflow.sdk.exceptions import AirflowSkipException
    with pytest.raises(AirflowSkipException):
        full.run_load_step("unzip", "recall", params={"sources": ["maude"]}, ds="2026-10-25")
    assert recorded == []


def test_rebuild_flags_only_on_merge(full, recorded):
    p = {"sources": ["maude"], "rebuild_prod": True}
    full.run_load_step("merge", "maude", params=p, ds="2026-10-25")
    full.run_load_step("staging", "maude", params=p, ds="2026-10-25")
    merge, staging = recorded
    assert "--rebuild" in merge and "--yes-rebuild" in merge and "2026-10-25" in merge
    assert "--rebuild" not in staging


def test_full_load_check_args(full):
    v = full.full_load_args("validate", "recall", "2026-10-26")
    assert v[0] == "scripts/validation/run_validation.py"
    assert "--values" in v and "2026-10-26" in v and "recall" in v
    c = full.full_load_args("check", "recall", "2026-10-26")
    assert c[0] == "scripts/warehouse/check_staging.py" and "--values" not in c
