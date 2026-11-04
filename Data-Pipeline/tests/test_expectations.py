"""Unit tests for scripts/validation/expectations.py. No GCP calls."""
import datetime as dt

import pytest

from scripts.validation import expectations as ex
from scripts.warehouse import schemas


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_expected_schema_follows_schemas_py(source):
    names = [n for n, _, _ in ex.expected_schema(source)]
    want = [n for n, _, _ in schemas.FLAT_COLUMNS[source] + schemas.STAGING_EXTRA]
    assert names == want


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_compare_schema_identical_is_clean(source):
    exp = ex.expected_schema(source)
    assert ex.compare_schema(exp, exp) == []


def test_compare_schema_accepts_bigquery_type_aliases():
    exp = ex.expected_schema("maude")                          # device_seq INT64
    live = [(n, "INTEGER" if n == "device_seq" else t, m) for n, t, m in exp]
    assert ex.compare_schema(exp, live) == []


def test_compare_schema_reports_missing_extra_type_and_mode():
    exp = ex.expected_schema("recall")
    live = [c for c in exp if c[0] != "root_cause"]            # missing
    live.append(("new_field", "STRING", "NULLABLE"))           # extra
    live = [(n, "STRING" if n == "record_date" else t,         # type change
             "REQUIRED" if n == "action" else m) for n, t, m in live]   # mode change
    problems = ex.compare_schema(exp, live)
    assert problems == sorted([
        "missing column: root_cause",
        "unexpected column: new_field",
        "type of record_date: expected DATE, found STRING",
        "mode of action: expected NULLABLE, found REQUIRED",
    ])


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_rules_reference_real_columns(source):
    cols = {n for n, _, _ in schemas.FLAT_COLUMNS[source]}
    rules = ex.RULES[source]
    referenced = set(rules.get("dates", []))
    for kind in ("in_set", "regex", "lengths"):
        referenced |= set(rules.get(kind, {}))
    assert referenced <= cols, f"{source}: unknown columns {referenced - cols}"


def test_columns_for_query_skips_repeated_columns():
    assert "product_codes" not in ex.columns_for_query("gudid")
    assert set(schemas.MERGE_KEYS["maude"]) <= set(ex.columns_for_query("maude"))


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_build_suite_contents(source):
    pytest.importorskip("great_expectations")
    suite = ex.build_suite(source, today=dt.date(2026, 10, 26))
    kinds = [type(e).__name__ for e in suite.expectations]
    assert kinds.count("ExpectColumnValuesToNotBeNull") == len(schemas.MERGE_KEYS[source])
    unique = ("ExpectCompoundColumnsToBeUnique" if len(schemas.MERGE_KEYS[source]) > 1
              else "ExpectColumnValuesToBeUnique")
    assert unique in kinds
    assert "ExpectColumnValuesToBeBetween" in kinds               # date range
    assert suite.name == f"{source}_stg"
