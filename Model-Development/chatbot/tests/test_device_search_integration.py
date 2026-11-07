"""Integration: question → device_search → query layer, on vigil_md_dev.

Marked "integration" (run by hand: pytest -m integration). Needs the dev index file
(python -m device_search.build_index) and BigQuery credentials; skips otherwise.
"""
import json
from pathlib import Path

import pytest

from device_search import DeviceIndex
from device_search.paths import index_path
from query_layer import BigQueryBackend, Budget, device_summary

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
        b.client.get_dataset(f"{b.project}.{DATASET}")
    except NotFound:
        pytest.skip(f"{DATASET} does not exist")
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


def test_index_and_query_layer_agree_on_linked_reports(index, backend):
    hit = index.resolve("dreamstation").matches[0]
    k = index.query_key(hit)
    out = device_summary(backend, k["key_type"], k["key"], Budget(max_gb=0.5),
                         recall_key=k["recall_key"])
    assert out["counts"]["total_reports"] == hit.reports > 0
    assert out["counts"]["by_event_type"].get("Death", 0) == hit.deaths
    assert out["recalls"]["matched_by"] == ["brand", "dreamstation"] and out["recalls"]["total"] > 0
