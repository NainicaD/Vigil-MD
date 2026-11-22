"""Shared rules for the data-check steps (validate, check) in the warehouse DAGs.

Used by vigil_md_load and vigil_md_full_load, so both handle check failures the
same way and CHECKS_ENFORCE is one switch for both. Not a DAG: listed in
dags/.airflowignore so Airflow does not parse it for DAGs.

validate = schema + Great Expectations (scripts/validation/run_validation.py)
check    = anomaly checks (scripts/warehouse/check_staging.py)
Both exit 3 when they find a data problem. That is not retried, because the
data would be the same; other errors are retried as usual.
"""
from airflow.sdk.exceptions import AirflowFailException

EXIT_DATA_FAIL = 3
CHECK_STEPS = {"validate", "check"}

# False = warn-only: a check failure is logged and the merge still runs.
# Set True once thresholds are tuned, so a failure stops that source's merge.
CHECKS_ENFORCE = True    # on since 2026-10-27, after the checks passed on real loads

CHECK_SCRIPTS = {
    "validate": "scripts/validation/run_validation.py",
    "check":    "scripts/warehouse/check_staging.py",
}
VALIDATION_RESULTS_DIR = "/opt/airflow/logs/validation"   # outside the repo
STATISTICS_DIR = "/opt/airflow/logs/statistics"           # outside the repo
VALIDATE_EXTRA_ARGS = ["--values", "--max-gb", "2", "--results-dir", VALIDATION_RESULTS_DIR,
                       "--stats", "--stats-max-gb", "5", "--out-dir", STATISTICS_DIR]


class DataCheckFailed(AirflowFailException):
    """A data check found a problem (exit 3). Fails without retrying."""


def warn_only(step: str) -> bool:
    """True if a DataCheckFailed from this step should only be logged."""
    return step in CHECK_STEPS and not CHECKS_ENFORCE


# ── Scheduling shared by the warehouse DAGs ───────────────────────────────────
# Two one-slot pools that run at the same time:
#   vigil_upload     unzip: GCS uploads share one connection, so one at a time.
#                    Unzip only touches its own source's manifests, partition
#                    state and NDJSON files, and never BigQuery.
#   vigil_warehouse  load_raw, staging, merge: BigQuery cancels concurrent
#                    transactions that mutate the same table (load_log).
# validate and check only read (SELECT), which BigQuery allows during a
# transaction, so they use default_pool.
UPLOAD_POOL = "vigil_upload"
WAREHOUSE_POOL = "vigil_warehouse"
READ_ONLY_POOL = "default_pool"

# Small sources first, so they are not stuck behind a long upload. With the
# upstream rule a task's effective priority is its own weight plus all its
# ancestors', so a source's next step outranks the next source's earlier steps
# and each source's chain finishes before the next one's.
SOURCE_PRIORITY = {"enforcement": 4, "recall": 3, "gudid": 2, "maude": 1}
WEIGHT_RULE = "upstream"


def pool_for(step: str) -> str:
    """The pool for one warehouse step."""
    if step == "unzip":
        return UPLOAD_POOL
    if step in CHECK_STEPS:
        return READ_ONLY_POOL
    return WAREHOUSE_POOL


# ── MAUDE event tables (problem terms, narrative text) ────────────────────────
# Three extra steps at the end of the MAUDE chain, after merge_maude, in both
# warehouse DAGs: build_maude_events.py --step staging | problems | text.
# They write BigQuery, so they use the vigil_warehouse pool (pool_for).
EVENT_STEPS = ["events_staging", "events_problems", "events_text"]
EVENT_SOURCE = "maude"
EVENTS_SCRIPT = "scripts/warehouse/build_maude_events.py"


def steps_for(source: str, steps: list[str]) -> list[str]:
    """The warehouse steps of one source's chain. Pure function."""
    return steps + (EVENT_STEPS if source == EVENT_SOURCE else [])


SUMMARY_TASK = "build_summaries"
SUMMARY_SCRIPT = "scripts/warehouse/build_summaries.py"


def summary_args(project: str, if_changed: bool = True) -> list[str]:
    """CLI args for the daily summary build (after every source's chain). Pure function."""
    return [SUMMARY_SCRIPT, "--project", project] + (["--if-changed"] if if_changed else [])


LINKS_TASK = "build_links"
LINKS_SCRIPT = "scripts/warehouse/build_links.py"


def links_args(project: str, if_changed: bool = True) -> list[str]:
    """CLI args for the exact link tables (after every source's chain). Pure function."""
    return [LINKS_SCRIPT, "--project", project] + (["--if-changed"] if if_changed else [])


MATCHING_DIR = "../Model-Development/matching"          # relative to Data-Pipeline/ (run_script's cwd)
CANDIDATES_SCRIPT = f"{MATCHING_DIR}/scripts/s13_build_candidates.py"
SCORE_SCRIPT = f"{MATCHING_DIR}/scripts/s12_score_pairs.py"
MODEL_LINKS_SCRIPT = "scripts/warehouse/load_model_links.py"
# Downloaded by hand from Artifact Registry (package matching-same-device); see the DAG docstring.
MATCHING_BUNDLE = f"{MATCHING_DIR}/models/matching-same-device-v1.1.0"
MATCHING_WORK_DIR = "/opt/airflow/logs/matching"         # outside the repo, shared by the workers
EXIT_NOTHING_TO_DO = 4                                   # s13: no candidate pairs


def matching_files(run_id: str, work_dir: str = MATCHING_WORK_DIR) -> dict[str, str]:
    """The files one vigil_md_matching run passes from task to task. Pure function."""
    d = f"{work_dir}/{run_id}"
    return {"candidates": f"{d}/candidates.parquet", "siblings": f"{d}/siblings.csv",
            "links": f"{d}/links.parquet", "report": f"{d}/report.json"}


def candidates_args(project: str, dataset: str, full: bool, out: str, siblings: str) -> list[str]:
    """CLI args for s13 (candidate pairs and their sibling keys). Pure function."""
    return ([CANDIDATES_SCRIPT, "--project", project, "--dataset", dataset, "--out", out,
             "--siblings-out", siblings] + (["--full"] if full else ["--since", "auto"]))


def score_args(pairs: str, siblings: str, bundle: str, out: str, report: str, workers: int,
               baseline: str | None = None) -> list[str]:
    """CLI args for s12 (score the pairs). Pure function."""
    return ([SCORE_SCRIPT, "--pairs", pairs, "--siblings", siblings, "--bundle", bundle,
             "--out", out, "--report", report, "--workers", str(workers)]
            + (["--baseline", baseline] if baseline else []))


def model_links_args(project: str, dataset: str, links: str, report: str, full: bool,
                     run_id: str) -> list[str]:
    """CLI args for load_model_links.py. Pure function."""
    return ([MODEL_LINKS_SCRIPT, "--project", project, "--dataset", dataset, "--links", links,
             "--report", report, "--run-id", run_id] + (["--full"] if full else []))


def events_args(step: str, project: str, ingest_date: str) -> list[str]:
    """CLI args for one MAUDE event step. Pure function."""
    return [EVENTS_SCRIPT, "--project", project, "--ingest-date", ingest_date,
            "--step", step.removeprefix("events_")]
