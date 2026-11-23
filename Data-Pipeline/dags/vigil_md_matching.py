"""Vigil MD — Matching DAG: score MAUDE ↔ GUDID candidate pairs with the matching model

Started by vigil_md_load after each daily load (task trigger_matching, conf dataset vigil_md_prod),
or by hand. The default dataset is vigil_md_prod; give conf {"dataset": "vigil_md_dev"} for the
test extract. The first prod run is a backfill by hand: full=true (docs/MATCHING_RUNBOOK.md).

Tasks:
  1. candidates        — s13_build_candidates.py: MAUDE rows with no usable UDI link that changed
                         since the last load (or all: full=true, or the link table was made by
                         another bundle/gate) → vigil_md_model.match_candidates_<dataset> and a
                         parquet file (streamed), plus the candidates' sibling keys (siblings.csv,
                         for feat_conflicting_identifier). No candidates (exit 4) → the rest is skipped.
  2. score             — s12_score_pairs.py: the model decides each pair (N worker processes,
                         memory one chunk each); MATCH rows → links parquet, run numbers →
                         report.json. Python only. Each chunk is saved (parts/) and logged
                         (chunks.jsonl), so its 2 retries continue from the last saved chunk.
  3. load_links        — load_model_links.py: links → <dataset>.link_maude_gudid_model, the
                         links view, the table label model_version, and ELK records: one
                         "matching_chunk_scored" per chunk and "matching_run_metrics".
                         --full when the run scored every row (full=true or s13's full.flag).
  4. build_summaries   — build_summaries.py --if-changed: entity_reports / entity_counts include
                         the new model links, so the chatbot and the website show them. Only
                         for a dataset in vigil_checks.MODEL_LINK_SUMMARY_DATASETS (vigil_md_dev;
                         vigil_md_prod once the chatbot that labels model links is deployed);
                         for other datasets it changes nothing.
  candidates, score, load_links and build_summaries send "matching_task_failed" /
  "matching_task_retry" to ELK when they fail or retry (vigil_helpers callbacks).
  5. alert_on_failure  — triggered only if any task fails
  6. mark_run_failed   — like the alert, then fails, so the run is "failed"

Files between tasks: MATCHING_WORK_DIR/<run_id>/ (Data-Pipeline/logs on the host).

Before the first run (docs/MATCHING_RUNBOOK.md):
  - download the bundle once (the repo is mounted, so the workers see it):
      gcloud artifacts generic download --package matching-same-device --version v1.1.0 \\
          --repository vigil-md-models --location us-central1 \\
          --destination Model-Development/matching/models/matching-same-device-v1.1.0
  - add xgboost and scikit-learn to _PIP_ADDITIONAL_REQUIREMENTS in .env (see .env.example)

Params: dataset, full (backfill / new model version), workers, bundle, ie_gate (the IE gate s12
applies; default M1 on top of v1.1.0 until a bundle with M1 is released), baseline (an earlier
report.json: adds score_psi to this run's report).
"""
from __future__ import annotations

import json
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
    EXIT_NOTHING_TO_DO, MATCHING_BUNDLE, MATCHING_DATASET, MATCHING_IE_GATE, READ_ONLY_POOL,
    WAREHOUSE_POOL, candidates_args, matching_files, model_links_args, model_version, score_args,
    summary_args)
from vigil_helpers import (  # noqa: E402
    REPO, alert_on_failure, mark_run_failed, matching_task_failed, matching_task_retry, run_script)

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
            "bundle": p["bundle"], "ie_gate": p["ie_gate"] or None, "baseline": p["baseline"] or None,
            "files": matching_files(context["run_id"])}


def _model_version(p: dict) -> str:
    """bundle version (its meta.json) + the gate this run applies."""
    meta = json.loads((REPO / p["bundle"] / "meta.json").read_text())
    return model_version(meta, p["ie_gate"] or meta.get("ie_gate", "e6_e3b"))


def build_candidates(**context):
    p = _params(context)
    f = p["files"]
    Path(f["candidates"]).parent.mkdir(parents=True, exist_ok=True)
    try:
        run_script(*candidates_args(PROJECT, p["dataset"], p["full"], f["candidates"], f["siblings"],
                                    model_version=_model_version(p)))
    except RuntimeError as e:
        if f"(exit {EXIT_NOTHING_TO_DO})" in str(e):
            raise AirflowSkipException("no new candidate pairs: nothing to score") from None
        raise


def score_pairs(**context):
    p = _params(context)
    f = p["files"]
    run_script(*score_args(f["candidates"], f["siblings"], p["bundle"], f["links"], f["report"],
                           p["workers"], f["dir"], p["baseline"], p["ie_gate"]))


def load_links(**context):
    p = _params(context)
    f = p["files"]
    # s13 writes full.flag when it scored every pool row (e.g. the link table was made by another
    # version): then every old link must go, not only those of the rows scored again
    full = p["full"] or Path(f["dir"], "full.flag").exists()
    run_script(*model_links_args(PROJECT, p["dataset"], f["links"], f["report"], f["chunks"],
                                 full, context["run_id"], _model_version(p)))


def build_summaries(**context):
    run_script(*summary_args(PROJECT, if_changed=True, dataset=_params(context)["dataset"]))


with DAG(
    dag_id="vigil_md_matching",
    description="Vigil MD — score MAUDE ↔ GUDID candidate pairs and load the model links",
    default_args=default_args,
    start_date=datetime(2026, 11, 1),
    schedule=None,          # started by vigil_md_load.trigger_matching, or by hand
    catchup=False,
    max_active_runs=1,
    params={
        "dataset":  Param(MATCHING_DATASET, type="string",
                          description="vigil_md_prod, or vigil_md_dev (test extract)"),
        "full":     Param(False, type="boolean",
                          description="score every MAUDE row of the pool (backfill, new model)"),
        "workers":  Param(2, type="integer", minimum=1,
                          description="scoring processes; each holds one copy of the bundle"),
        "bundle":   Param(MATCHING_BUNDLE, type="string",
                          description="released bundle folder, relative to Data-Pipeline/"),
        "ie_gate":  Param(MATCHING_IE_GATE, type="string",
                          description="IE gate applied on top of the bundle; empty = the bundle's own"),
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
    summaries = PythonOperator(task_id="build_summaries", python_callable=build_summaries,
                               pool=WAREHOUSE_POOL, **ELK_CALLBACKS)

    alert = PythonOperator(task_id="alert_on_failure", python_callable=alert_on_failure,
                           trigger_rule=TriggerRule.ONE_FAILED)
    mark_failed = PythonOperator(task_id="mark_run_failed", python_callable=mark_run_failed,
                                 trigger_rule=TriggerRule.ONE_FAILED)

    candidates >> score >> links >> summaries
    [candidates, score, links, summaries] >> alert
    [candidates, score, links, summaries] >> mark_failed
