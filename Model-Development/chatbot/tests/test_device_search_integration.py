"""Integration: question → device_search → query layer, on vigil_md_dev.

Marked "integration" (run by hand: pytest -m integration). Needs the dev index file
(python -m device_search.build_index), the dev summary tables and BigQuery credentials; skips
otherwise.
"""
import json
from pathlib import Path

import pytest

from device_search import DeviceIndex
from device_search.paths import index_path
from query_layer import BigQueryBackend, Budget, device_summary, identity

pytestmark = pytest.mark.integration
DATASET = json.loads((Path(__file__).resolve().parents[1] / "data" / "dev_extract.json")
                     .read_text())["target_dataset"]


@pytest.fixture(scope="module")
def index():
    if not index_path(DATASET).exists():
        pytest.skip("no dev index: run python -m device_search.build_index")
    return DeviceIndex.open(DATASET)


@pytest.fixture(scope="module")
def backend():
    from google.api_core.exceptions import NotFound
    b = BigQueryBackend(dataset=DATASET)
    try:
        b.client.get_table(f"{b.project}.{DATASET}.entity_counts")
    except NotFound:
        pytest.skip(f"{DATASET} has no summary tables")
    except Exception as e:
        pytest.skip(f"BigQuery not reachable: {type(e).__name__}")
    return b


@pytest.mark.parametrize("question, title", [
    ("How many deaths were reported for the dreamstaton?", "DreamStation"),
    ("what problems are reported for the mentor memorygel", "MENTOR MemoryGel Breast Implant"),
    ("Was the Philips DreamStation recalled?", "DreamStation"),
])
def test_questions_resolve_on_real_data(index, question, title):
    r = index.resolve(question)
    assert r.status == "resolved" and r.matches[0].title == title


def test_question_to_answer_matches_the_raw_udi_links(index, backend):
    from google.cloud import bigquery
    hit = index.resolve("dreamstation").matches[0]
    out = device_summary(backend, **index.query_key(hit), budget=Budget(max_gb=0.2))
    raw = list(backend.client.query(
        f"SELECT COUNT(DISTINCT source_record_id) AS n FROM {backend.table('maude')} "
        f"WHERE udi_di IN UNNEST(@udis)",
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000, query_parameters=[
            bigquery.ArrayQueryParameter("udis", "STRING", index.udis_for(hit))])).result())[0].n
    assert out["counts"]["total_reports"] == hit.reports == raw > 0
    assert out["counts"]["by_event_type"].get("Death", 0) == hit.deaths
    assert out["recalls"]["total"] > 0 and out["entity"]["title"] == "DreamStation"


def test_device_identity(index, backend):
    hit = index.resolve("dreamstation").matches[0]
    out = identity(backend, **index.query_key(hit), budget=Budget(max_gb=0.2))
    assert out["details"]["name"] == "DreamStation" and "BZD" in out["details"]["product_codes"]
