"""Integration tests: the query layer on the real development dataset (vigil_md_dev).

Marked "integration": skipped by a normal pytest run and in CI. Run by hand (needs gcloud
application-default credentials and the dataset built by scripts/build_dev_extract.py):

    pytest -m integration -q

Each check compares the query layer's answer with simple, independent SQL on the same
dataset, so the tests hold after a rebuild. Cost: each query bills BigQuery's 10 MB minimum,
about 0.2-0.3 GB for the whole file.
"""
import json
from pathlib import Path

import pytest

from query_layer import Budget, BigQueryBackend, device_summary, identity, narratives
from query_layer.terms import NO_INFORMATION

pytestmark = pytest.mark.integration

CONFIG = json.loads((Path(__file__).resolve().parents[1] / "data" / "dev_extract.json").read_text())
DATASET = CONFIG["target_dataset"]
CAP_GB = 0.5      # the dev dataset is small: a whole question must stay far below this


@pytest.fixture(scope="module")
def backend():
    pytest.importorskip("google.cloud.bigquery")
    from google.api_core.exceptions import NotFound
    b = BigQueryBackend(dataset=DATASET)
    try:
        b.client.get_dataset(f"{b.project}.{DATASET}")
    except NotFound:
        pytest.skip(f"{DATASET} does not exist: run scripts/build_dev_extract.py --yes")
    except Exception as e:                       # no credentials, offline
        pytest.skip(f"BigQuery not reachable: {type(e).__name__}")
    return b


def scalar(backend, sql, **params):
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=200_000_000, query_parameters=[
        bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()])
    return list(backend.client.query(sql, job_config=cfg).result())[0][0]


@pytest.fixture(scope="module")
def lzg(backend):
    budget = Budget(max_gb=CAP_GB)
    return device_summary(backend, "product_code", "LZG", budget), budget


def test_report_counts_match_independent_sql(backend, lzg):
    out, _ = lzg
    expected = scalar(backend, f"SELECT COUNT(DISTINCT source_record_id) FROM "
                               f"{backend.table('maude')} WHERE product_code = @c", c="LZG")
    assert out["counts"]["total_reports"] == expected > 0
    deaths = scalar(backend, f"SELECT COUNT(DISTINCT source_record_id) FROM {backend.table('maude')} "
                             f"WHERE product_code = @c AND event_type = 'Death'", c="LZG")
    assert out["counts"]["by_event_type"].get("Death", 0) == deaths


def test_problems_are_ranked_and_leave_out_no_information_terms(lzg):
    out, _ = lzg
    for kind in ("device", "patient"):
        terms = out["problems"][kind]
        assert len(terms) <= 10
        assert [t["reports"] for t in terms] == sorted((t["reports"] for t in terms), reverse=True)
        assert not {t["term"] for t in terms} & set(NO_INFORMATION)
    assert 0 <= out["problems"]["no_harm_reports"] <= out["counts"]["total_reports"]


def test_recalls_match_independent_sql(backend, lzg):
    out, _ = lzg
    expected = scalar(backend, f"SELECT COUNT(*) FROM {backend.table('recall')} "
                               f"WHERE product_code = @c", c="LZG")
    assert out["recalls"]["total"] == expected
    assert {r["recall_class"] for r in out["recalls"]["shown"]} <= {"Class I", "Class II",
                                                                  "Class III", None}


def test_whole_question_is_cheap_and_cites_its_sources(lzg):
    out, budget = lzg
    assert budget.spent_gb < CAP_GB
    assert [s["query"] for s in out["sources"]] == ["counts", "problems", "recalls"]
    assert set(out["data_as_of"]) == {"maude", "maude_problems", "recall", "enforcement"}


def test_device_without_recalls(backend):
    out = device_summary(backend, "product_code", "PKU", Budget(max_gb=CAP_GB))
    assert out["recalls"]["total"] == 0 and out["recalls"]["shown"] == []
    assert out["counts"]["total_reports"] > 0


def test_narratives_for_a_death_heavy_device(backend):
    out = narratives(backend, "product_code", "OZD", n=5, max_chars=600, budget=Budget(max_gb=CAP_GB))
    ex = out["excerpts"]
    assert 0 < len(ex) <= 5 and ex[0]["event_type"] == "Death"
    assert all(len(e["text"]) <= 601 for e in ex)
    assert len({e["mdr_report_key"] for e in ex}) == len(ex)


def test_identity_by_product_code(backend):
    out = identity(backend, "product_code", "LZG", Budget(max_gb=CAP_GB))
    assert out["devices"] and all("LZG" in d["product_codes"].split(",") for d in out["devices"])
