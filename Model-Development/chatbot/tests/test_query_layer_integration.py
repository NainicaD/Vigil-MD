"""Integration tests: the query layer on the real development dataset (vigil_md_dev).

Marked "integration": skipped by a normal pytest run and in CI. Run by hand (needs gcloud
application-default credentials, the dataset from scripts/build_dev_extract.py and its summary
tables from Data-Pipeline/scripts/warehouse/build_summaries.py --dataset vigil_md_dev):

    pytest -m integration -q

The query layer reads the summary tables; each check recomputes the same answer with simple,
independent SQL on the raw tables (maude, maude_problems, recall), so the pipeline's summaries
and the query layer are tested together. Cost: BigQuery bills at least 10 MB per query, about
0.3-0.5 GB for the whole file.
"""
import json
import sys
from pathlib import Path

import pytest

from query_layer import Budget, BigQueryBackend, device_summary, identity, narratives

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "data" / "dev_extract.json").read_text())
DATASET = CONFIG["target_dataset"]
sys.path.insert(0, str(ROOT.parents[1] / "Data-Pipeline"))
from scripts.warehouse.summary_config import NO_HARM, NO_INFORMATION  # noqa: E402  (one list, the pipeline's)

CAP_GB = 0.2      # a whole question on the dev dataset stays far below this


@pytest.fixture(scope="module")
def backend():
    pytest.importorskip("google.cloud.bigquery")
    from google.api_core.exceptions import NotFound
    b = BigQueryBackend(dataset=DATASET)
    try:
        b.client.get_table(f"{b.project}.{DATASET}.entity_counts")
    except NotFound:
        pytest.skip(f"{DATASET} has no summary tables: run build_summaries.py --dataset {DATASET}")
    except Exception as e:                       # no credentials, offline
        pytest.skip(f"BigQuery not reachable: {type(e).__name__}")
    return b


def sql(backend, text, **params):
    from google.cloud import bigquery
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=300_000_000, query_parameters=[
        bigquery.ArrayQueryParameter(k, "STRING", v) if isinstance(v, list)
        else bigquery.ScalarQueryParameter(k, "STRING", v) for k, v in params.items()])
    return [dict(r) for r in backend.client.query(text, job_config=cfg).result()]


@pytest.fixture(scope="module")
def lzg(backend):
    budget = Budget(max_gb=CAP_GB)
    return device_summary(backend, "product_code", "LZG", budget), budget


def test_report_counts_match_the_raw_maude_table(backend, lzg):
    out, _ = lzg
    raw = sql(backend, f"""SELECT IFNULL(event_type, '(none)') AS et, COUNT(*) AS n FROM (
        SELECT source_record_id, ANY_VALUE(event_type) AS event_type FROM {backend.table('maude')}
        WHERE product_code = @c GROUP BY 1) GROUP BY 1""", c="LZG")
    assert out["counts"]["by_event_type"] == {r["et"]: r["n"] for r in raw}
    assert out["entity"]["found"] and out["entity"]["title"] == "Product code LZG"


def test_top_problems_match_the_raw_tables(backend, lzg):
    out, _ = lzg
    raw = sql(backend, f"""SELECT p.problem_type, p.problem_term, COUNT(DISTINCT p.mdr_report_key) AS n
        FROM {backend.table('maude_problems')} AS p
        JOIN (SELECT DISTINCT source_record_id FROM {backend.table('maude')} WHERE product_code = @c) AS m
          ON m.source_record_id = p.mdr_report_key
        WHERE p.problem_term NOT IN UNNEST(@skip)
        GROUP BY 1, 2""", c="LZG", skip=list(NO_INFORMATION + NO_HARM))
    for kind in ("device", "patient"):
        expected = sorted(((r["problem_term"], r["n"]) for r in raw if r["problem_type"] == kind),
                          key=lambda t: (-t[1], t[0]))[:10]
        assert [(t["term"], t["reports"]) for t in out["problems"][kind]] == expected, kind
        assert not {t["term"] for t in out["problems"][kind]} & set(NO_INFORMATION)


def test_recalls_match_the_raw_recall_table(backend, lzg):
    out, _ = lzg
    raw = sql(backend, f"SELECT COUNT(*) AS n FROM {backend.table('recall')} WHERE product_code = @c", c="LZG")
    assert out["recalls"]["total"] == raw[0]["n"] > 0
    assert {r["recall_class"] for r in out["recalls"]["shown"]} <= {"Class I", "Class II", "Class III", None}


def test_a_whole_question_is_cheap_and_cites_its_sources(lzg):
    out, budget = lzg
    assert budget.spent_gb < CAP_GB
    assert [s["query"] for s in out["sources"]] == ["catalog", "counts", "problems", "recalls"]


def test_device_without_recalls(backend):
    out = device_summary(backend, "product_code", "PKU", Budget(max_gb=CAP_GB))
    assert out["recalls"]["total"] == 0 and out["counts"]["total_reports"] > 0


def test_narratives_for_a_death_heavy_product_code(backend):
    out = narratives(backend, "product_code", "OZD", n=5, max_chars=600, budget=Budget(max_gb=CAP_GB))
    ex = out["excerpts"]
    assert 0 < len(ex) <= 5 and ex[0]["event_type"] == "Death"
    assert all(len(e["text"]) <= 601 for e in ex) and len({e["mdr_report_key"] for e in ex}) == len(ex)


def test_identity_of_a_product_code(backend):
    out = identity(backend, "product_code", "LZG", Budget(max_gb=CAP_GB))
    assert out["entity"]["found"] and out["details"]["top_names"]
