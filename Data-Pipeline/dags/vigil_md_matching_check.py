"""Vigil MD — Weekly UDI agreement check of the matching model

Every Monday 07:00 UTC, on vigil_md_prod. It starts unpaused (is_paused_upon_creation=False). Some MAUDE rows
have an exact UDI link to GUDID, so their device is known. The model never sees the UDI (a banned
feature), so these rows test it. Read the numbers as a trend: rows with a UDI are easier than the
pool. Nothing is loaded into a link table.

Tasks:
  0. sync_bundle       — as in vigil_md_matching: the newest published bundle, or the param bundle.
  1. candidates        — s13 --mode udi_check: a fixed sample (sample_per_10k) of UDI-linked rows
                         → vigil_md_model.match_candidates_check_<dataset> (never the pool's table)
                         and a parquet file, with true_group_key and sibling keys.
  2. score             — s12, with checkpoints (2 retries continue from the last chunk).
  3. evaluate          — s14_udi_agreement.py → check_report.json: precision, recall,
                         candidate recall, model recall.
  4. send_report       — one ELK record "matching_udi_agreement".
  5. decide_retrain    — precision below retrain_precision (default 0.90, the Kibana alert level)
                         starts the GitHub workflow model-training.yml on retrain_ref (it retrains
                         and publishes only a model that passes the gate), at most once per 7 days;
                         e-mail and ELK record "matching_retrain_triggered". No retry: a retry
                         could start the workflow twice.
  6. alert_on_failure, mark_run_failed — as in vigil_md_matching.

Files between tasks: CHECK_WORK_DIR/<run_id>/. candidates, score and evaluate send
"matching_task_failed" / "matching_task_retry" to ELK.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.sdk import Param
from airflow.sdk.exceptions import AirflowSkipException
from airflow.providers.standard.operators.python import PythonOperator
from airflow.task.trigger_rule import TriggerRule

sys.path.insert(0, str(Path(__file__).resolve().parent))   # dags/ (shared modules)
from vigil_checks import (  # noqa: E402
    CHECK_WORK_DIR, EXIT_NOTHING_TO_DO, MATCHING_DATASET, MATCHING_IE_GATE,
    READ_ONLY_POOL, RETRAIN_PACKAGE, RETRAIN_PRECISION, RETRAIN_REF, RETRAIN_SEMVER, WAREHOUSE_POOL,
    candidates_args, evaluate_args, matching_files, retrain_reason, score_args)
from vigil_helpers import (  # noqa: E402
    BUNDLE_PACKAGE, alert_on_failure, bundle_folder, choose_bundle, mark_run_failed,
    matching_task_failed, matching_task_retry, retrain_if_needed, run_script, send_to_elk)

PROJECT = "vigil-md-1792696816"
ELK_CALLBACKS = {"on_failure_callback": matching_task_failed,
                 "on_retry_callback": matching_task_retry}

default_args = {
    "owner":            "matching",
    "retries":          1,
    "retry_delay":      timedelta(minutes=5),
    "email_on_failure": False,
}


def _params(context) -> dict:
    p = context["params"]
    return {"dataset": p["dataset"], "sample_per_10k": int(p["sample_per_10k"]),
            "bundle": bundle_folder(context), "ie_gate": p["ie_gate"] or None,
            "files": matching_files(context["run_id"], CHECK_WORK_DIR)}


def build_candidates(**context):
    p = _params(context)
    f = p["files"]
    Path(f["dir"]).mkdir(parents=True, exist_ok=True)
    try:
        run_script(*candidates_args(PROJECT, p["dataset"], False, f["candidates"], f["siblings"],
                                    sample_per_10k=p["sample_per_10k"]))
    except RuntimeError as e:
        if f"(exit {EXIT_NOTHING_TO_DO})" in str(e):
            raise AirflowSkipException("no candidate pairs in the sample") from None
        raise


def score_pairs(**context):
    p = _params(context)
    f = p["files"]
    run_script(*score_args(f["candidates"], f["siblings"], p["bundle"], f["links"], f["report"],
                           1, f["dir"], ie_gate=p["ie_gate"]))


def evaluate(**context):
    p = _params(context)
    f = p["files"]
    run_script(*evaluate_args(f["candidates"], f["links"], p["bundle"], f["check_report"], p["ie_gate"]))


def send_report(**context):
    p = _params(context)
    report = json.loads(Path(p["files"]["check_report"]).read_text())
    send_to_elk("matching_udi_agreement", {**report, "dataset": p["dataset"],
                                           "sample_per_10k": p["sample_per_10k"],
                                           "run_id": context["run_id"]})


def decide_retrain(**context):
    p = _params(context)
    report = json.loads(Path(p["files"]["check_report"]).read_text())
    floor = float(context["params"]["retrain_precision"])
    reason = retrain_reason(precision=report.get("precision"), precision_floor=floor)
    params = context["params"]
    return retrain_if_needed(reason, context, params["retrain_ref"],
                             {"precision": report.get("precision"), "precision_floor": floor,
                              "dataset": p["dataset"]},
                             params["retrain_package"], params["retrain_semver"])


with DAG(
    dag_id="vigil_md_matching_check",
    description="Vigil MD — weekly UDI agreement check of the matching model",
    default_args=default_args,
    start_date=datetime(2026, 11, 1),
    schedule="0 7 * * 1",
    # the weekly quality signal for prod: runs without a manual unpause on a new laptop
    # (docker-compose.yaml pauses new DAGs by default)
    is_paused_upon_creation=False,
    catchup=False,
    max_active_runs=1,
    params={
        "dataset":        Param(MATCHING_DATASET, type="string",
                                description="vigil_md_prod, or vigil_md_dev (test extract)"),
        "sample_per_10k": Param(100, type="integer", minimum=1, maximum=10000,
                                description="UDI-linked MAUDE rows sampled per 10,000 (100 = 1%)"),
        "bundle":         Param("", type="string",
                                description="empty = the newest published version (task sync_bundle); "
                                            "a folder relative to Data-Pipeline/ = that one (rollback)"),
        "bundle_package": Param(BUNDLE_PACKAGE, type="string",
                                description="sync_bundle uses the newest version of this package; "
                                            "a test: matching-same-device-test"),
        "ie_gate":        Param(MATCHING_IE_GATE, type="string",
                                description="IE gate applied on top of the bundle; empty = the bundle's own"),
        "retrain_precision": Param(RETRAIN_PRECISION, type="number", minimum=0, maximum=1,
                                   description="retrain when precision is below this (0.99 = a demo trigger)"),
        "retrain_ref":    Param(RETRAIN_REF, type="string",
                                description="the branch the triggered model-training run trains from"),
        "retrain_package": Param(RETRAIN_PACKAGE, type="string",
                                 description="where a passing model is published; a test: matching-same-device-test"),
        "retrain_semver": Param(RETRAIN_SEMVER, type="string",
                                description="auto = newest + 1 patch; a new test package needs e.g. 0.0.1"),
    },
    tags=["vigil-md", "matching", "model", "monitoring"],
) as dag:

    sync = PythonOperator(task_id="sync_bundle", python_callable=choose_bundle, **ELK_CALLBACKS)
    candidates = PythonOperator(task_id="candidates", python_callable=build_candidates,
                                pool=WAREHOUSE_POOL, **ELK_CALLBACKS)
    score = PythonOperator(task_id="score", python_callable=score_pairs, pool=READ_ONLY_POOL,
                           retries=2, **ELK_CALLBACKS)
    check = PythonOperator(task_id="evaluate", python_callable=evaluate, pool=READ_ONLY_POOL,
                           **ELK_CALLBACKS)
    send = PythonOperator(task_id="send_report", python_callable=send_report)
    retrain = PythonOperator(task_id="decide_retrain", python_callable=decide_retrain, retries=0,
                             **ELK_CALLBACKS)

    alert = PythonOperator(task_id="alert_on_failure", python_callable=alert_on_failure,
                           trigger_rule=TriggerRule.ONE_FAILED)
    mark_failed = PythonOperator(task_id="mark_run_failed", python_callable=mark_run_failed,
                                 trigger_rule=TriggerRule.ONE_FAILED)

    sync >> candidates >> score >> check >> send >> retrain
    [sync, candidates, score, check, send, retrain] >> alert
    [sync, candidates, score, check, send, retrain] >> mark_failed
