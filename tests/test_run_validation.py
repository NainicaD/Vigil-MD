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
