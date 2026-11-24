"""Shared rules for the data-check steps (validate, check) in the warehouse DAGs.

Used by vigil_md_load and vigil_md_full_load, so both handle check failures the
same way and CHECKS_ENFORCE is one switch for both. Not a DAG: listed in
dags/.airflowignore so Airflow does not parse it for DAGs.

validate = schema + Great Expectations (scripts/validation/run_validation.py)
check    = anomaly checks (scripts/warehouse/check_staging.py)
Both exit 3 when they find a data problem. That is not retried, because the
data would be the same; other errors are retried as usual.
"""
from datetime import datetime, timedelta

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


# The datasets whose summary tables include the matching model's links (build_summaries.py
# --model-links). Add vigil_md_prod only after the chatbot that labels model links ("... with
# this device's UDI, ... matched by device details") is deployed: older chatbot code would call every linked
# report "linked by UDI". Until then model links can be loaded into prod without the live site
# showing them. After adding it, rebuild the prod summaries once without --if-changed.
MODEL_LINK_SUMMARY_DATASETS = frozenset({"vigil_md_dev"})
SUMMARY_DEFAULT_DATASET = "vigil_md_prod"                # build_summaries.py without --dataset


def summary_args(project: str, if_changed: bool = True, dataset: str | None = None) -> list[str]:
    """CLI args for the summary build (after every source's chain, and after a load of model
    links). --model-links only for a dataset in MODEL_LINK_SUMMARY_DATASETS. Pure function."""
    model_links = (dataset or SUMMARY_DEFAULT_DATASET) in MODEL_LINK_SUMMARY_DATASETS
    return ([SUMMARY_SCRIPT, "--project", project] + (["--dataset", dataset] if dataset else [])
            + (["--if-changed"] if if_changed else [])
            + (["--model-links"] if model_links else []))


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
CHECK_WORK_DIR = f"{MATCHING_WORK_DIR}/check"            # vigil_md_matching_check: never mixed
WORK_DIRS = {"vigil_md_matching": MATCHING_WORK_DIR, "vigil_md_matching_check": CHECK_WORK_DIR}
# The reference score distribution for drift (s12 --baseline -> score_psi): the report.json of the
# prod backfill with the bundle and gate above. s12 skips the PSI for a run of another bundle or gate,
# or of fewer than 1,000 pairs. Without the file, vigil_md_matching scores without a baseline.
# Replace the file after a new bundle's first full run.
MATCHING_BASELINE = f"{MATCHING_DIR}/reports/score_baseline.json"
EVALUATE_SCRIPT = f"{MATCHING_DIR}/scripts/s14_udi_agreement.py"
EXIT_NOTHING_TO_DO = 4                                   # s13: no candidate pairs
MATCHING_DATASET = "vigil_md_prod"                       # dev runs: conf {"dataset": "vigil_md_dev"}
# shortcut: remove after the next bundle release names e6_e3b_c1_m1 in meta.json (docs/EXPERIMENTS.md M1);
# until then s12 applies the M1 gate on top of the published v1.1.0 bundle.
MATCHING_IE_GATE = "e6_e3b_c1_m1"


# ── Retraining trigger (Model Deployment guidelines 5.3) ──────────────────────
# vigil_md_matching_check (precision) and vigil_md_matching (drift) start the GitHub workflow
# model-training.yml when a threshold is crossed: it retrains, gates and publishes a better model
# only. At most one started run per RETRAIN_COOLDOWN_DAYS, so a check that stays low does not
# retrain every week. The token is VIGIL_GITHUB_TOKEN in .env (Actions: read and write).
RETRAIN_PRECISION = 0.90        # the same level as the Kibana alert "UDI agreement precision below 0.90"
RETRAIN_PSI = 0.2               # the same level as s12 PSI_WARN and the Kibana drift rule
RETRAIN_COOLDOWN_DAYS = 7
RETRAIN_REPO = "NainicaD/Vigil-MD"
RETRAIN_WORKFLOW = "model-training.yml"
RETRAIN_REF = "main"            # the branch the triggered run trains from; DAG param retrain_ref


def retrain_reason(precision: float | None = None, psi: float | None = None,
                   precision_floor: float = RETRAIN_PRECISION, psi_max: float = RETRAIN_PSI) -> str | None:
    """Why the model should be retrained, or None. precision: the weekly UDI check; psi: a run's
    score_psi (None when s12 skipped it). Pure function."""
    reasons = []
    if precision is not None and precision < precision_floor:
        reasons.append(f"UDI precision {precision:.3f} < {precision_floor:.2f}")
    if psi is not None and psi > psi_max:
        reasons.append(f"score drift (PSI) {psi:.3f} > {psi_max:.2f}")
    return "; ".join(reasons) or None


def recent_run(runs: list[dict], now: datetime, days: int = RETRAIN_COOLDOWN_DAYS) -> dict | None:
    """The newest GitHub workflow run created less than `days` ago, or None. runs: the "workflow_runs"
    of the GitHub API (created_at like "2026-11-24T13:44:33Z"). Pure function."""
    def created(r):
        return datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
    recent = [r for r in runs if now - created(r) < timedelta(days=days)]
    return max(recent, key=created) if recent else None


def dispatch_body(ref: str, reason: str) -> dict:
    """The body of the workflow_dispatch call: train from ref and publish if the gate passes. Pure."""
    return {"ref": ref, "inputs": {"reason": reason[:200], "publish": "true"}}


def model_version(bundle_meta: dict, ie_gate: str) -> str:
    """What made a run's links: bundle version + gate. Stamped on the link table, compared by s13
    --since auto (another version → a full rescore). Pure function."""
    return f"{bundle_meta.get('version', 'unknown')}+{ie_gate}"


def matching_files(run_id: str, work_dir: str = MATCHING_WORK_DIR) -> dict[str, str]:
    """The files one vigil_md_matching run passes from task to task. Pure function."""
    d = f"{work_dir}/{run_id}"
    return {"dir": d, "candidates": f"{d}/candidates.parquet", "siblings": f"{d}/siblings.csv",
            "links": f"{d}/links.parquet", "report": f"{d}/report.json",
            "chunks": f"{d}/chunks.jsonl", "check_report": f"{d}/check_report.json"}


def candidates_args(project: str, dataset: str, full: bool, out: str, siblings: str,
                    sample_per_10k: int | None = None, model_version: str | None = None) -> list[str]:
    """CLI args for s13 (candidate pairs and their sibling keys). With sample_per_10k: the
    udi_check sample instead of the pool. model_version: a link table made by another version
    means a full rescore (s13 --since auto). Pure function."""
    args = [CANDIDATES_SCRIPT, "--project", project, "--dataset", dataset, "--out", out,
            "--siblings-out", siblings]
    if sample_per_10k is not None:
        return args + ["--mode", "udi_check", "--sample-per-10k", str(sample_per_10k)]
    return (args + (["--full"] if full else ["--since", "auto"])
            + (["--model-version", model_version] if model_version else []))


def evaluate_args(candidates: str, links: str, bundle: str, out: str,
                  ie_gate: str | None = None) -> list[str]:
    """CLI args for s14 (the UDI agreement check). Pure function."""
    return ([EVALUATE_SCRIPT, "--candidates", candidates, "--links", links, "--bundle", bundle,
             "--out", out] + (["--ie-gate", ie_gate] if ie_gate else []))


def score_args(pairs: str, siblings: str, bundle: str, out: str, report: str, workers: int,
               work_dir: str, baseline: str | None = None, ie_gate: str | None = None) -> list[str]:
    """CLI args for s12 (score the pairs; checkpoints in work_dir, so a retry continues). Pure."""
    return ([SCORE_SCRIPT, "--pairs", pairs, "--siblings", siblings, "--bundle", bundle,
             "--out", out, "--report", report, "--workers", str(workers), "--work-dir", work_dir]
            + (["--baseline", baseline] if baseline else [])
            + (["--ie-gate", ie_gate] if ie_gate else []))


def model_links_args(project: str, dataset: str, links: str, report: str, chunks: str, full: bool,
                     run_id: str, model_version: str | None = None) -> list[str]:
    """CLI args for load_model_links.py. Pure function."""
    return ([MODEL_LINKS_SCRIPT, "--project", project, "--dataset", dataset, "--links", links,
             "--report", report, "--chunks", chunks, "--run-id", run_id] + (["--full"] if full else [])
            + (["--model-version", model_version] if model_version else []))


def events_args(step: str, project: str, ingest_date: str) -> list[str]:
    """CLI args for one MAUDE event step. Pure function."""
    return [EVENTS_SCRIPT, "--project", project, "--ingest-date", ingest_date,
            "--step", step.removeprefix("events_")]
