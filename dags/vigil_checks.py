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
CHECKS_ENFORCE = False

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
