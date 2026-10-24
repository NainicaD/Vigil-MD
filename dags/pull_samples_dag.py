"""Airflow DAG wrapping scripts/acquisition/pull_samples.py.

Each data source is its own task (a "bucket"), fanned out with dynamic task
mapping so sources run in parallel and can later be scattered across workers
or moved to separate DAGs/operators independently. A final task writes the
summary once all buckets are done.

Trigger with config to override defaults, e.g.:
    {"limit": 100, "product_code": "FRN"}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pendulum
from airflow.sdk import Param, dag, task, get_current_context

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = PROJECT_ROOT / "data" / "raw" / "samples"

# One entry per bucket. Add new sources here; each becomes a parallel task.
BUCKETS = [
    {"name": "enforcement", "kind": "openfda", "endpoint": "enforcement"},
    {"name": "recall", "kind": "openfda", "endpoint": "recall"},
    {"name": "maude", "kind": "openfda", "endpoint": "event", "scoped": True},
    {"name": "gudid", "kind": "gudid"},
]


def _load_module(product_code: str):
    """Import pull_samples lazily so DAG parsing has no side effects."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.acquisition import pull_samples as ps

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ps.OUT = OUT_DIR  # absolute path, independent of worker cwd
    ps.PRODUCT_CODE = product_code
    return ps


@dag(
    dag_id="pull_samples",
    description="Pull FDA/GUDID sample records, one parallel task per source",
    start_date=pendulum.datetime(2025, 1, 1, tz="UTC"),
    schedule=None,  # manual trigger; fixtures only
    catchup=False,
    max_active_tasks=4,
    params={
        "limit": Param(200, type="integer", minimum=1, maximum=200),
        "product_code": Param("FRN", type="string"),
    },
    tags=["acquisition", "vigil-md"],
)
def pull_samples_dag():
    @task
    def list_buckets() -> list[dict]:
        return BUCKETS

    @task(retries=2, retry_delay=pendulum.duration(minutes=1))
    def pull_bucket(bucket: dict) -> dict:
        params = get_current_context()["params"]
        limit = min(int(params["limit"]), 200)
        code = params["product_code"]
        ps = _load_module(code)
        s = ps.make_session()

        if bucket["kind"] == "gudid":
            maude_records = ps.pull_openfda(s, "event", limit, search=f'device.device_report_product_code:"{code}"')
            records = ps.pull_gudid(s, maude_records, limit)
        else:
            search = ""
            if bucket.get("scoped"):
                search = f'device.device_report_product_code:"{code}"'
            records = ps.pull_openfda(s, bucket["endpoint"], limit, search=search)

        ps.save(bucket["name"], records)
        return {"name": bucket["name"], "count": len(records)}

    @task
    def write_summary(results: list[dict]) -> str:
        params = get_current_context()["params"]
        summary = {"product_code": params["product_code"]}
        summary.update({r["name"]: r["count"] for r in results})
        summary["note"] = (
            "These are development fixtures only. "
            "Not a representative sample. "
            "Replace with full scoped snapshot before modelling."
        )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / "summary.json"
        path.write_text(json.dumps(summary, indent=2))
        return str(path)

    results = pull_bucket.expand(bucket=list_buckets())
    write_summary(results)


pull_samples_dag()
