"""Vigil MD — Matching DAG: score MAUDE ↔ GUDID candidate pairs with the matching model

Not scheduled, and no other DAG starts it yet: trigger it manually. The default dataset is
vigil_md_dev (the test extract). Moving it to vigil_md_prod (and a trigger from vigil_md_load)
is a separate task.

Tasks:
  1. candidates        — s13_build_candidates.py: MAUDE rows with no usable UDI link that changed
                         since the last load (or all, full=true) → vigil_md_model.match_candidates_<dataset>
                         and a parquet file, plus the candidates' sibling keys (siblings.csv,
                         for feat_conflicting_identifier). No candidates (exit 4) → the rest is skipped.
  2. score             — s12_score_pairs.py: the model decides each pair (N worker processes);
                         MATCH rows → links parquet, run numbers → report.json. Python only.
                         Each chunk is saved (parts/) and logged (chunks.jsonl), so its 2
                         retries continue from the last saved chunk.
  3. load_links        — load_model_links.py: links → <dataset>.link_maude_gudid_model, the
                         links view, and ELK records: one "matching_chunk_scored" per chunk and
                         "matching_run_metrics" (report, link counts, storage sizes).
  candidates, score and load_links send "matching_task_failed" / "matching_task_retry" to ELK
  when they fail or retry (vigil_helpers callbacks).
  4. alert_on_failure  — triggered only if any task fails
  5. mark_run_failed   — like the alert, then fails, so the run is "failed"

Files between tasks: MATCHING_WORK_DIR/<run_id>/ (Data-Pipeline/logs on the host).

Before the first run:
  - download the bundle once (the repo is mounted, so the workers see it):
      gcloud artifacts generic download --package matching-same-device --version v1.1.0 \\
          --repository vigil-md-models --location us-central1 \\
          --destination Model-Development/matching/models/matching-same-device-v1.1.0
  - add xgboost and scikit-learn to _PIP_ADDITIONAL_REQUIREMENTS in .env (see .env.example)

Params: dataset, full (backfill / new model version), workers, bundle, baseline (an earlier
report.json: adds score_psi to this run's report).
"""
from __future__ import annotations

import logging
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
    EXIT_NOTHING_TO_DO, MATCHING_BUNDLE, READ_ONLY_POOL, WAREHOUSE_POOL, candidates_args,
    matching_files, model_links_args, score_args)
from vigil_helpers import (  # noqa: E402
    alert_on_failure, mark_run_failed, matching_task_failed, matching_task_retry, run_script)

log = logging.getLogger(__name__)

PROJECT = "vigil-md-1792696816"
# On the working tasks only: mark_run_failed fails on purpose and must not add a record.
ELK_CALLBACKS = {"on_failure_callback": matching_task_failed,
                 "on_retry_callback": matching_task_retry}
SCORE_RETRIES = 2       # each retry continues from the last saved chunk (s12 --work-dir)

default_args = {
    "owner":            "matching",
    "retries":          1,
    "retry_delay":      timedelta(minutes=5),
    "email_on_failure": False,
}


def _params(context) -> dict:
    p = context["params"]
    return {"dataset": p["dataset"], "full": bool(p["full"]), "workers": int(p["workers"]),
            "bundle": p["bundle"], "baseline": p["baseline"] or None,
            "files": matching_files(context["run_id"])}


def build_candidates(**context):
    p = _params(context)
    f = p["files"]
    Path(f["candidates"]).parent.mkdir(parents=True, exist_ok=True)
    try:
        run_script(*candidates_args(PROJECT, p["dataset"], p["full"], f["candidates"], f["siblings"]))
    except RuntimeError as e:
        if f"(exit {EXIT_NOTHING_TO_DO})" in str(e):
            raise AirflowSkipException("no new candidate pairs: nothing to score") from None
        raise


def score_pairs(**context):
    p = _params(context)
    f = p["files"]
    run_script(*score_args(f["candidates"], f["siblings"], p["bundle"], f["links"], f["report"],
                           p["workers"], f["dir"], p["baseline"]))


def load_links(**context):
    p = _params(context)
    f = p["files"]
    run_script(*model_links_args(PROJECT, p["dataset"], f["links"], f["report"], f["chunks"],
                                 p["full"], context["run_id"]))


with DAG(
    dag_id="vigil_md_matching",
    description="Vigil MD — score MAUDE ↔ GUDID candidate pairs and load the model links",
    default_args=default_args,
    start_date=datetime(2026, 11, 1),
    schedule=None,          # manual until the move to PROD
    catchup=False,
    max_active_runs=1,
    params={
        "dataset":  Param("vigil_md_dev", type="string",
                          description="vigil_md_dev (test extract) or vigil_md_prod"),
        "full":     Param(False, type="boolean",
                          description="score every MAUDE row of the pool (backfill, new model)"),
        "workers":  Param(2, type="integer", minimum=1,
                          description="scoring processes; each holds one copy of the bundle"),
        "bundle":   Param(MATCHING_BUNDLE, type="string",
                          description="released bundle folder, relative to Data-Pipeline/"),
        "baseline": Param("", type="string",
                          description="an earlier report.json for score_psi; empty = none"),
    },
    tags=["vigil-md", "matching", "model"],
) as dag:

    candidates = PythonOperator(task_id="candidates", python_callable=build_candidates,
                                pool=WAREHOUSE_POOL, **ELK_CALLBACKS)
    score = PythonOperator(task_id="score", python_callable=score_pairs, pool=READ_ONLY_POOL,
                           retries=SCORE_RETRIES, **ELK_CALLBACKS)
    links = PythonOperator(task_id="load_links", python_callable=load_links, pool=WAREHOUSE_POOL,
                           **ELK_CALLBACKS)

    alert = PythonOperator(task_id="alert_on_failure", python_callable=alert_on_failure,
                           trigger_rule=TriggerRule.ONE_FAILED)
    mark_failed = PythonOperator(task_id="mark_run_failed", python_callable=mark_run_failed,
                                 trigger_rule=TriggerRule.ONE_FAILED)

    candidates >> score >> links
    [candidates, score, links] >> alert
    [candidates, score, links] >> mark_failed
