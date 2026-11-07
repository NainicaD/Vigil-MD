"""Vigil MD — Daily loading DAG: GCS → RAW → STAGING → PROD

Not scheduled. vigil_md_acquisition starts it with its last task
(trigger_load), only after acquisition is done, and passes
conf {"ingest_date": "<acquisition ds>"}. See docs/WAREHOUSE_PLAN.md (Step 10).

Tasks:
  1. load.*            — per source: unzip → load_raw → staging → validate →
                         check → merge (--scope changed; pool vigil_warehouse,
                         one BigQuery writer at a time). A source with nothing
                         pending is a quick no-op. MAUDE then builds its event
                         tables: events_staging → events_problems → events_text
                         (build_maude_events.py).
  2. build_summaries   — after every chain (also when one failed: the others' data is new):
                         rebuild the daily summary tables if any source table changed
                         (build_summaries.py --if-changed)
  3. alert_on_failure  — triggered only if any task fails

Manual trigger: set the ingest_date param (YYYY-MM-DD), or leave it empty to
use the run's own date. The full load / backfill is vigil_md_full_load.py.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.sdk import Param, TaskGroup
from airflow.providers.standard.operators.python import PythonOperator
from airflow.task.trigger_rule import TriggerRule

sys.path.insert(0, str(Path(__file__).resolve().parent))   # dags/ (shared modules)
from vigil_checks import (  # noqa: E402
    CHECK_SCRIPTS, EVENT_STEPS, SOURCE_PRIORITY, SUMMARY_TASK, VALIDATE_EXTRA_ARGS, WEIGHT_RULE,
    DataCheckFailed, events_args, pool_for, steps_for, summary_args, warn_only)
from vigil_helpers import alert_on_failure, run_script  # noqa: E402,F401  (shared, issue #14)

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BUCKET     = "vigil-md-raw-nd"
PROJECT    = "vigil-md-1792696816"
WAREHOUSE_POOL = "vigil_warehouse"     # 1 slot: one BigQuery writer at a time
SOURCES    = ["enforcement", "recall", "maude", "gudid"]

default_args = {
    "owner":            "nainica",
    "retries":          2,
    "retry_delay":      timedelta(minutes=5),
    "email_on_failure": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

WAREHOUSE_STEPS = {
    # step: (script, takes --bucket, takes --scope)
    "unzip":    ("scripts/warehouse/unzip_to_ndjson.py", True,  True),
    "load_raw": ("scripts/warehouse/load_raw_to_bq.py",  True,  True),
    "staging":  ("scripts/warehouse/build_staging.py",   False, False),
    "validate": (CHECK_SCRIPTS["validate"],              False, False),
    "check":    (CHECK_SCRIPTS["check"],                 False, False),
    "merge":    ("scripts/warehouse/merge_prod.py",      False, False),
}


def warehouse_args(step: str, source: str, ingest_date: str,
                   scope: str = "changed") -> list[str]:
    """CLI args for one warehouse script. Pure function."""
    if step in EVENT_STEPS:
        return events_args(step, PROJECT, ingest_date)
    script, takes_bucket, takes_scope = WAREHOUSE_STEPS[step]
    args = [script, "--source", source, "--ingest-date", ingest_date]
    if step != "unzip":
        args += ["--project", PROJECT]
    if takes_bucket:
        args += ["--bucket", BUCKET]
    if takes_scope:
        args += ["--scope", scope]
    if step == "validate":
        args += VALIDATE_EXTRA_ARGS
    return args


def resolve_ingest_date(context: dict) -> str:
    """ingest_date from the trigger conf / param, else the run's ds. Pure function.

    Reads dag_run.conf directly too, so it does not depend on the
    dag_run_conf_overrides_params setting.
    """
    conf = getattr(context.get("dag_run"), "conf", None) or {}
    value = (conf.get("ingest_date")
             or (context.get("params") or {}).get("ingest_date")
             or context.get("ds"))
    if not value:
        raise ValueError("No ingest_date: trigger with conf {'ingest_date': 'YYYY-MM-DD'}")
    return date.fromisoformat(str(value)).isoformat()


# ── Tasks ─────────────────────────────────────────────────────────────────────

def run_load_step(step: str, source: str, **context):
    """Run one warehouse script for one source."""
    ingest_date = resolve_ingest_date(context)
    try:
        run_script(*warehouse_args(step, source, ingest_date))
    except DataCheckFailed:
        if warn_only(step):
            log.warning("%s %s found data problems (warn-only, CHECKS_ENFORCE is False): "
                        "the merge continues. See the check output above.", step, source)
            return
        raise
    log.info("%s %s completed successfully (ingest_date %s)", step, source, ingest_date)


def build_summaries(**context):
    """Rebuild the daily summary tables when a source table changed."""
    run_script(*summary_args(PROJECT))
    log.info("summary tables up to date")


# ── DAG ───────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="vigil_md_load",
    description="Vigil MD — Load GCS → RAW → STAGING → PROD (started by vigil_md_acquisition)",
    default_args=default_args,
    start_date=datetime(2026, 10, 1),
    schedule=None,          # started by vigil_md_acquisition.trigger_load
    catchup=False,
    max_active_runs=1,
    params={
        "ingest_date": Param(None, type=["null", "string"],
                             description="YYYY-MM-DD. Set by trigger_load; empty = the run's date"),
    },
    tags=["vigil-md", "warehouse", "load"],
) as dag:

    # One chain per source; chains are independent (a MAUDE failure does not
    # block recall). The 1-slot pool is shared with vigil_md_full_load.
    with TaskGroup("load", dag=dag) as load_group:
        for source in SOURCES:
            chain = [
                PythonOperator(
                    task_id=f"{step}_{source}",
                    python_callable=run_load_step,
                    op_kwargs={"step": step, "source": source},
                    pool=pool_for(step),
                    priority_weight=SOURCE_PRIORITY[source],
                    weight_rule=WEIGHT_RULE,
                )
                for step in steps_for(source, list(WAREHOUSE_STEPS))
            ]
            for upstream, downstream in zip(chain, chain[1:]):
                upstream >> downstream

    summaries = PythonOperator(
        task_id=SUMMARY_TASK,
        python_callable=build_summaries,
        trigger_rule=TriggerRule.ALL_DONE,      # runs even if one source's chain failed
        pool=WAREHOUSE_POOL,
    )

    alert = PythonOperator(
        task_id="alert_on_failure",
        python_callable=alert_on_failure,
        trigger_rule=TriggerRule.ONE_FAILED,
    )

    load_group >> summaries
    [load_group, summaries] >> alert
