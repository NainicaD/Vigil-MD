"""Unit tests for scripts/validation/run_validation.py, with a fake BigQuery client."""
from types import SimpleNamespace

from scripts.validation import run_validation as rv
from scripts.validation.expectations import expected_schema
from scripts.warehouse import schemas

PROJECT = "test-project"


class FakeClient:
    """get_table returns a table whose schema is expected_schema, optionally altered."""
    def __init__(self, drop=None):
        self.drop = drop

    def get_table(self, table_id):
        source = table_id.split(".")[-1].removesuffix("_stg")
        fields = [SimpleNamespace(name=n, field_type=t, mode=m)
                  for n, t, m in expected_schema(source) if n != self.drop]
        return SimpleNamespace(schema=fields)


def test_check_schemas_all_match():
    assert rv.check_schemas(FakeClient(), PROJECT, schemas.SOURCES) is True


def test_check_schemas_reports_a_missing_column():
    assert rv.check_schemas(FakeClient(drop="record_date"), PROJECT, ["recall"]) is False


def test_live_schema_reads_name_type_mode():
    cols = rv.live_schema(FakeClient(), schemas.staging_table(PROJECT, "gudid"))
    assert ("product_codes", "STRING", "REPEATED") in cols


# ── Saved value-check results ─────────────────────────────────────────────────
import datetime as dt  # noqa: E402


def _fake_result(success=True):
    cfg = SimpleNamespace(type="expect_column_values_to_be_between",
                          kwargs={"column": "record_date", "batch_id": "x",
                                  "min_value": dt.date(1970, 1, 1),
                                  "max_value": dt.date(2026, 10, 27)})
    return SimpleNamespace(expectation_config=cfg, success=success,
                           result={"unexpected_count": 0, "unexpected_percent": 0.0})


def test_check_entry_keeps_rule_and_counts():
    e = rv.check_entry(_fake_result())
    assert e["expectation"] == "expect_column_values_to_be_between"
    assert e["column"] == "record_date"
    assert e["rule"] == {"min_value": "1970-01-01", "max_value": "2026-10-27"}
    assert e["success"] is True and e["unexpected_count"] == 0


def test_validation_document_summarises_results():
    entries = [rv.check_entry(_fake_result(True)), rv.check_entry(_fake_result(False))]
    doc = rv.validation_document("p", "recall", dt.date(2026, 10, 26), entries)
    assert doc["table"] == "p.vigil_md_staging.recall_stg"
    assert (doc["passed"], doc["total"], doc["success"]) == (1, 2, False)



def test_run_stats_saves_nothing_for_an_empty_partition(tmp_path):
    class _Client:
        def query(self, sql):
            return SimpleNamespace(result=lambda: iter([{"_rows": 0}]))
    assert rv.run_stats(_Client(), "p", "recall", dt.date(2026, 10, 28), tmp_path) is None
    assert not any(tmp_path.rglob("*.json"))
