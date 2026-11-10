"""Vigil MD — Full load DAG (manual): GCS → RAW → STAGING → PROD

Trigger by hand for the first load, a backfill, a schema change or a recovery.
See docs/WAREHOUSE_PLAN.md (Step 8) and the Runbook.

Params:
  sources       which sources to load (default: all 4)
  refetch       first re-pull from the FDA with fetch_sources.py --mode full
                (default false: use the zips already in GCS)
  rebuild_prod  replace every prod row from all staging history
                (merge_prod.py --rebuild --yes-rebuild; default false: MERGE)

Tasks:
  1. guard_no_acquisition_run — fail if vigil_md_acquisition or vigil_md_load is running
  2. refetch                  — fetch_sources.py --mode full (only if refetch)
  3. load.*                   — per source: unzip --scope all → load_raw --scope all
                                --mode full → staging → validate → check → merge;
                                MAUDE then: events_staging → events_problems →
                                events_text (build_maude_events.py)
  4. build_summaries          — after every chain: rebuild the daily summary tables
                                (build_summaries.py, always: a full load changes the sources)
  5. build_links              — after every chain: rebuild the exact link tables and the
                                links view (build_links.py, always)
  6. alert_on_failure         — triggered only if any task fails

Every task that writes GCS state or BigQuery uses the 1-slot vigil_warehouse
pool, shared with vigil_md_acquisition.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow import DAG
from airflow.sdk import Param, TaskGroup
from airflow.sdk.exceptions import AirflowSkipException

sys.path.insert(0, str(Path(__file__).resolve().parent))   # dags/ (shared modules)
from vigil_checks import (  # noqa: E402
    CHECK_SCRIPTS, EVENT_STEPS, LINKS_TASK, SOURCE_PRIORITY, SUMMARY_TASK, VALIDATE_EXTRA_ARGS,
    WEIGHT_RULE, DataCheckFailed, events_args, links_args, pool_for, steps_for, summary_args,
    warn_only)
from vigil_helpers import alert_on_failure, run_script  # noqa: E402,F401  (shared, issue #14)
from airflow.providers.standard.operators.python import PythonOperator
from airflow.task.trigger_rule import TriggerRule

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BUCKET     = "vigil-md-raw-nd"
PROJECT    = "vigil-md-1792696816"
START_YEAR = 2016
END_YEAR   = 2026
WAREHOUSE_POOL = "vigil_warehouse"
SOURCES    = ["enforcement", "recall", "maude", "gudid"]
# Daily DAGs that write the same GCS state and BigQuery tables
DAILY_DAG_IDS = ["vigil_md_acquisition", "vigil_md_load"]

default_args = {
    "owner":            "nainica",
    "retries":          1,
    "retry_delay":      timedelta(minutes=5),
    "email_on_failure": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def full_load_args(step: str, source: str, ingest_date: str,
                   rebuild: bool = False) -> list[str]:
    """CLI args for one full-load script. Pure function."""
    if step in EVENT_STEPS:
        return events_args(step, PROJECT, ingest_date)
    if step == "unzip":
        return ["scripts/warehouse/unzip_to_ndjson.py", "--bucket", BUCKET,
                "--source", source, "--scope", "all", "--ingest-date", ingest_date]
    if step == "load_raw":
        return ["scripts/warehouse/load_raw_to_bq.py", "--project", PROJECT,
                "--bucket", BUCKET, "--source", source, "--scope", "all",
                "--ingest-date", ingest_date, "--mode", "full"]
    if step == "staging":
        return ["scripts/warehouse/build_staging.py", "--project", PROJECT,
                "--source", source, "--ingest-date", ingest_date]
    if step in ("validate", "check"):
        args = [CHECK_SCRIPTS[step], "--project", PROJECT,
                "--source", source, "--ingest-date", ingest_date]
        return args + (VALIDATE_EXTRA_ARGS if step == "validate" else [])
    if step == "merge":
        args = ["scripts/warehouse/merge_prod.py", "--project", PROJECT,
                "--source", source, "--ingest-date", ingest_date]
        return args + (["--rebuild", "--yes-rebuild"] if rebuild else [])
    raise ValueError(f"unknown step {step}")


LOAD_STEPS = ["unzip", "load_raw", "staging", "validate", "check", "merge"]


def selected_sources(params: dict) -> list[str]:
    """Validate the sources param. Pure function."""
    chosen = list(params.get("sources") or [])
    unknown = sorted(set(chosen) - set(SOURCES))
    if unknown or not chosen:
        raise ValueError(f"sources must be a non-empty subset of {SOURCES}, got {chosen}")
    return chosen


# ── Tasks ─────────────────────────────────────────────────────────────────────

def guard_no_acquisition_run(**context):
    """Fail if a daily DAG has a run in progress (they write the same state)."""
    selected_sources(context["params"])            # fail early on a bad param
    for daily in DAILY_DAG_IDS:
        n = context["ti"].get_dr_count(dag_id=daily, states=["running", "queued"])
        if n:
            raise RuntimeError(
                f"{daily} has {n} run(s) in progress. Wait for it to "
                "finish, or pause it, then trigger the full load again.")
    log.info("No run in progress for %s", ", ".join(DAILY_DAG_IDS))


def refetch(**context):
    """Re-pull the selected sources with --mode full, if the refetch param is set."""
    params = context["params"]
    if not params.get("refetch"):
        log.info("refetch=false — using the zips already in GCS")
        return
    for source in selected_sources(params):
        run_script(
            "scripts/acquisition/fetch_sources.py",
            "--bucket",     BUCKET,
            "--source",     source,
            "--start-year", str(START_YEAR),
            "--end-year",   str(END_YEAR),
            "--mode",       "full",
        )
        log.info("%s full fetch completed", source)


def load_date(context: dict) -> str:
    """The run's date (YYYY-MM-DD). Uses ds when the run has a logical date;
    a run triggered without one (e.g. from the CLI) has no ds, so fall back to
    the time the run was created."""
    if context.get("ds"):
        return context["ds"]
    return context["dag_run"].run_after.date().isoformat()


def run_load_step(step: str, source: str, **context):
    params = context["params"]
    if source not in selected_sources(params):
        raise AirflowSkipException(f"{source} not in sources param")
    rebuild = bool(params.get("rebuild_prod")) and step == "merge"
    try:
        run_script(*full_load_args(step, source, load_date(context), rebuild))
    except DataCheckFailed:
        if warn_only(step):
            log.warning("%s %s found data problems (warn-only, CHECKS_ENFORCE is False): "
                        "the merge continues. See the check output above.", step, source)
            return
        raise
    log.info("%s %s completed%s", step, source, " (rebuild)" if rebuild else "")


def build_summaries(**context):
    """Rebuild the daily summary tables after a full load."""
    run_script(*summary_args(PROJECT, if_changed=False))
    log.info("summary tables rebuilt")


def build_links(**context):
    """Rebuild the exact link tables after a full load."""
    run_script(*links_args(PROJECT, if_changed=False))
    log.info("link tables rebuilt")


# ── DAG ───────────────────────────────────────────────────────────────────────

with DAG(
    dag_id="vigil_md_full_load",
    description="Vigil MD — Manual full load: GCS → RAW → STAGING → PROD",
    default_args=default_args,
    start_date=datetime(2026, 10, 1),
    schedule=None,          # manual trigger only
    catchup=False,
    max_active_runs=1,
    params={
        "sources":      Param(SOURCES, type="array",
                              description=f"Subset of {SOURCES}"),
        "refetch":      Param(False, type="boolean",
                              description="Re-pull from the FDA first (fetch_sources.py --mode full)"),
        "rebuild_prod": Param(False, type="boolean",
                              description="Replace every prod row from all staging history"),
    },
    tags=["vigil-md", "warehouse", "full-load"],
) as dag:

    guard = PythonOperator(
        task_id="guard_no_acquisition_run",
        python_callable=guard_no_acquisition_run,
        retries=0,
    )

    fetch = PythonOperator(
        task_id="refetch",
        python_callable=refetch,
        pool=WAREHOUSE_POOL,
    )

    # One chain per source; sources not in the param are skipped.
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
                for step in steps_for(source, LOAD_STEPS)
            ]
            for upstream, downstream in zip(chain, chain[1:]):
                upstream >> downstream
            fetch >> chain[0]

    alert = PythonOperator(
        task_id="alert_on_failure",
        python_callable=alert_on_failure,
        trigger_rule=TriggerRule.ONE_FAILED,
    )

    summaries = PythonOperator(
        task_id=SUMMARY_TASK,
        python_callable=build_summaries,
        trigger_rule=TriggerRule.ALL_DONE,
        pool=WAREHOUSE_POOL,
    )

    links = PythonOperator(
        task_id=LINKS_TASK,
        python_callable=build_links,
        trigger_rule=TriggerRule.ALL_DONE,
        pool=WAREHOUSE_POOL,
    )

    guard >> fetch
    load_group >> summaries
    load_group >> links
    [guard, fetch, load_group, summaries, links] >> alert
