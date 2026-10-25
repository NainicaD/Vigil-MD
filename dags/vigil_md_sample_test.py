"""Vigil MD — Sample test DAG (manual): the warehouse on the sample records

Runs the real warehouse scripts on gs://vigil-md-raw-nd/samples/, in an
isolated setup. Production is never written:
  GCS       gs://vigil-md-test-nd
  BigQuery  vigil_md_raw_test, vigil_md_staging_test, vigil_md_prod_test, vigil_md_ops_test

VIGIL_BQ_DATASET_SUFFIX=_test is set only in the environment of the scripts
this DAG starts, never on the Airflow process (one process parses every DAG).
scripts/warehouse/sample_test.py also refuses to run without it.

Tasks:
  1. reset           empty the test bucket, drop the *_test datasets
  2. seed            sample NDJSON → openFDA bulk zips; GUDID XML sample zip
  3. create_tables   the 13 tables in the *_test datasets
  4. load.*          per source: unzip → load_raw → staging → merge (full-load args)
  5. check           counts per layer, load_log, GUDID fields, no leftovers
  6. scenarios       idempotency, content hash, one changed record

Test logs appear in Kibana under dag_id vigil_md_sample_test.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from airflow import DAG
from airflow.sdk import TaskGroup
from airflow.providers.standard.operators.python import PythonOperator

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REPO        = Path(__file__).resolve().parents[1]
PYTHON      = sys.executable
PROJECT     = "vigil-md-1792696816"
TEST_BUCKET = "vigil-md-test-nd"
TEST_ENV    = {"VIGIL_BQ_DATASET_SUFFIX": "_test"}
INGEST_DATE = "2026-10-25"            # same D1 as scripts/warehouse/sample_test.py
SOURCES     = ["enforcement", "recall", "maude", "gudid"]

default_args = {"owner": "nainica", "retries": 0}   # a test: fail fast, no retries


# ── Helpers ───────────────────────────────────────────────────────────────────

def run_test_script(script: str, *args: str) -> str:
    """Run a repo script with the _test suffix in its environment only."""
    cmd = [PYTHON, str(REPO / script)] + list(args)
    log.info("Running (VIGIL_BQ_DATASET_SUFFIX=_test): %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                            env={**os.environ, **TEST_ENV})
    if result.stdout:
        log.info(result.stdout)
    if result.stderr:
        log.warning(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"{script} failed (exit {result.returncode})\n{result.stderr[-3000:]}")
    return result.stdout


def test_load_args(step: str, source: str) -> list[str]:
    """The full-load arguments, pointed at the test bucket. Pure function."""
    if step == "unzip":
        return ["scripts/warehouse/unzip_to_ndjson.py", "--bucket", TEST_BUCKET,
                "--source", source, "--scope", "all", "--ingest-date", INGEST_DATE]
    if step == "load_raw":
        return ["scripts/warehouse/load_raw_to_bq.py", "--project", PROJECT, "--bucket", TEST_BUCKET,
                "--source", source, "--scope", "all", "--ingest-date", INGEST_DATE, "--mode", "full"]
    if step == "staging":
        return ["scripts/warehouse/build_staging.py", "--project", PROJECT,
                "--source", source, "--ingest-date", INGEST_DATE]
    if step == "merge":
        return ["scripts/warehouse/merge_prod.py", "--project", PROJECT,
                "--source", source, "--ingest-date", INGEST_DATE]
    raise ValueError(f"unknown step {step}")


LOAD_STEPS = ["unzip", "load_raw", "staging", "merge"]


def run_sample_step(step: str, **context):
    run_test_script("scripts/warehouse/sample_test.py", step)


def run_create_tables(**context):
    run_test_script("scripts/warehouse/create_tables.py", "--project", PROJECT)


def run_load_step(step: str, source: str, **context):
    run_test_script(*test_load_args(step, source))


# ── DAG ───────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="vigil_md_sample_test",
    description="Vigil MD — Warehouse test on sample records (test bucket + *_test datasets)",
    default_args=default_args,
    start_date=datetime(2026, 10, 1),
    schedule=None,          # manual trigger only
    catchup=False,
    max_active_runs=1,
    tags=["vigil-md", "warehouse", "test"],
) as dag:

    reset = PythonOperator(task_id="reset", python_callable=run_sample_step,
                           op_kwargs={"step": "reset"})
    seed = PythonOperator(task_id="seed", python_callable=run_sample_step,
                          op_kwargs={"step": "seed"})
    create_tables = PythonOperator(task_id="create_tables", python_callable=run_create_tables)

    # Sources run one after another: every raw load writes load_log inside a
    # transaction, and parallel transactions on one table can be aborted. (The
    # production DAGs use the 1-slot vigil_warehouse pool; this test DAG does not
    # need that pool to exist.)
    with TaskGroup("load", dag=dag) as load_group:
        previous = create_tables
        for source in SOURCES:
            chain = [
                PythonOperator(
                    task_id=f"{step}_{source}",
                    python_callable=run_load_step,
                    op_kwargs={"step": step, "source": source},
                )
                for step in LOAD_STEPS
            ]
            for upstream, downstream in zip(chain, chain[1:]):
                upstream >> downstream
            previous >> chain[0]
            previous = chain[-1]

    check = PythonOperator(task_id="check", python_callable=run_sample_step,
                           op_kwargs={"step": "check"})
    scenarios = PythonOperator(task_id="scenarios", python_callable=run_sample_step,
                               op_kwargs={"step": "scenarios"})

    reset >> seed >> create_tables
    load_group >> check >> scenarios
