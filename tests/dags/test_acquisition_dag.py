"""DAG tests for dags/pull_samples_dag.py.

A: import / parsing   D: BUCKETS config   L3: end-to-end local runs (dag.test()).
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.acquisition import load_interim_to_bq, upload_samples_to_gcs

DAGS_DIR = Path(__file__).resolve().parents[2] / "dags"


# ── A. import and parsing ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def dagbag():
    from airflow.models import DagBag
    return DagBag(dag_folder=str(DAGS_DIR))


def test_dag_files_import_without_errors(dagbag):
    assert dagbag.import_errors == {}


def test_pull_samples_dag_is_found(dagbag):
    assert "pull_samples" in dagbag.dags


def test_dag_ids_are_unique_across_dag_files():
    ids = {}
    for f in DAGS_DIR.glob("*.py"):
        for found in re.findall(r'dag_id\s*=\s*"([^"]+)"', f.read_text()):
            ids.setdefault(found, []).append(f.name)
    dupes = {k: v for k, v in ids.items() if len(v) > 1}
    assert not dupes, f"duplicate dag_ids across files: {dupes}"


def test_parsing_has_no_side_effects(tmp_path, monkeypatch):
    from airflow.models import DagBag
    monkeypatch.chdir(tmp_path)
    DagBag(dag_folder=str(DAGS_DIR))
    assert list(tmp_path.iterdir()) == []


# ── D. BUCKETS config ────────────────────────────────────────────────────────

def test_bucket_names_are_unique(dag_module):
    names = [b["name"] for b in dag_module.BUCKETS]
    assert len(names) == len(set(names))


def test_buckets_match_downstream_script_sources(dag_module):
    names = {b["name"] for b in dag_module.BUCKETS}
    assert names == set(upload_samples_to_gcs.SOURCES)
    assert names == set(load_interim_to_bq.SOURCES)


def test_bucket_kinds_are_valid(dag_module):
    assert {b["kind"] for b in dag_module.BUCKETS} <= {"openfda", "gudid"}


def test_openfda_buckets_have_endpoint_and_gudid_does_not(dag_module):
    for b in dag_module.BUCKETS:
        if b["kind"] == "openfda":
            assert b.get("endpoint"), f"{b['name']} needs an endpoint"
        else:
            assert "endpoint" not in b


def test_only_maude_is_scoped_by_product_code(dag_module):
    scoped = {b["name"] for b in dag_module.BUCKETS if b.get("scoped")}
    assert scoped == {"maude"}


# ── L3. execution (dag.test, HTTP faked, output in tmp dir) ──────────────────

def run_dag(dag_module, **conf):
    from airflow.dag_processing.bundles.manager import DagBundlesManager
    from airflow.dag_processing.dagbag import DagBag, sync_bag_to_db

    dag = dag_module.pull_samples_dag()
    for task in dag.tasks:  # keep retries, drop the 1-minute wait
        task.retry_delay = timedelta(0)
    DagBundlesManager().sync_bundles_to_db()
    bag = DagBag(collect_dags=False)
    bag.bag_dag(dag)
    sync_bag_to_db(bag, "dags-folder", None)
    return dag, dag.test(run_conf=conf or None)


def states(run):
    return {ti.task_id: ti.state for ti in run.get_task_instances()}


def test_full_run_succeeds_with_one_mapped_task_per_bucket(dag_module, fake_http):
    dag, run = run_dag(dag_module)
    assert run.state == "success"
    mapped = [ti for ti in run.get_task_instances() if ti.task_id == "pull_bucket"]
    assert len(mapped) == len(dag_module.BUCKETS)
    assert all(ti.state == "success" for ti in mapped)
    assert states(run)["write_summary"] == "success"


def test_output_files_written_for_every_bucket(dag_module, fake_http):
    run_dag(dag_module)
    out = dag_module.OUT_DIR
    for b in dag_module.BUCKETS:
        data = json.loads((out / f"{b['name']}_sample.json").read_text())
        assert data["records"], f"{b['name']} has no records"
    summary = json.loads((out / "summary.json").read_text())
    assert summary["product_code"] == "FRN"
    for b in dag_module.BUCKETS:
        assert summary[b["name"]] > 0


def test_param_overrides_reach_tasks(dag_module, fake_http):
    run_dag(dag_module, limit=2, product_code="XYZ")
    openfda_calls = [p for url, p in fake_http.calls if "api.fda.gov" in url]
    assert openfda_calls and all(p["limit"] <= 2 for p in openfda_calls)
    assert any('"XYZ"' in p.get("search", "") for p in openfda_calls)
    summary = json.loads((dag_module.OUT_DIR / "summary.json").read_text())
    assert summary["product_code"] == "XYZ"


def test_only_maude_requests_carry_product_code_search(dag_module, fake_http):
    run_dag(dag_module)
    assert fake_http.calls, "no HTTP requests were made"
    for url, params in fake_http.calls:
        if "event.json" in url:
            assert 'device.device_report_product_code:"FRN"' == params["search"]
        elif "api.fda.gov" in url:
            assert "search" not in params


def test_failure_in_one_bucket_fails_run_and_skips_summary(dag_module, fake_http):
    fake_http.fail_for = ("recall",)
    _, run = run_dag(dag_module)
    assert run.state == "failed"
    assert states(run)["write_summary"] != "success"
    assert not (dag_module.OUT_DIR / "summary.json").exists()


def test_zero_records_fails_loudly(dag_module, fake_http):
    fake_http.n = 0
    _, run = run_dag(dag_module)
    assert run.state == "failed"
    assert not (dag_module.OUT_DIR / "summary.json").exists()
