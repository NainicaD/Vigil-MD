"""Tests for scripts/warehouse/build_staging.py and sql/staging_*.sql.

The SQL is checked as text. BigQuery is the fake_gcp MagicMock. The SQL-vs-Python
parity check needs real BigQuery and stays manual (docs/TESTS_PLAN.md TD4).
"""
import re
import sys
import types

import pytest

from scripts.warehouse import build_staging as B
from scripts.warehouse import schemas


def render(source):
    return B.render_staging_sql(source, schemas.raw_table("p", source),
                                schemas.staging_table("p", source), B.read_source_sql(source))


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_source_sql_is_valid(source):
    B.validate_source_sql(source, B.read_source_sql(source))


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_insert_columns_match_schemas(source):
    sql = render(source)
    cols = re.search(r"INSERT INTO `[^`]+` \((.+?)\)", sql).group(1).split(", ")
    want = [n for n, _, _ in schemas.FLAT_COLUMNS[source]] + [n for n, _, _ in schemas.STAGING_EXTRA]
    assert cols == want


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_reads_one_raw_partition_and_writes_one_staging_partition(source):
    sql = render(source)
    assert "__RAW_TABLE__" not in sql
    assert f"FROM `{schemas.raw_table('p', source)}`" in sql
    assert f"DELETE FROM `{schemas.staging_table('p', source)}` WHERE _ingest_date = @ingest_date" in sql
    assert re.search(r"WHERE (r\.)?_ingest_date = @ingest_date", sql)


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_dedup_and_null_key_filter(source):
    sql = render(source)
    keys = schemas.MERGE_KEYS[source]
    assert f"PARTITION BY {', '.join(keys)} ORDER BY _loaded_at DESC, record_hash) = 1" in sql
    assert "WHERE " + " AND ".join(f"{k} IS NOT NULL" for k in keys) in sql


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_statement_order(source):
    sql = render(source)
    assert sql.startswith("DECLARE")
    order = ["CREATE TEMP FUNCTION norm_date", "CREATE TEMP TABLE flat", "BEGIN TRANSACTION",
             "DELETE FROM", "INSERT INTO", "COMMIT TRANSACTION"]
    positions = [sql.index(s) for s in order]
    assert positions == sorted(positions)


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_record_hash_covers_business_columns_only(source):
    sql = render(source)
    struct = re.search(r"STRUCT\((.+?)\)\)\)\) AS record_hash", sql).group(1).split(", ")
    assert struct == [n for n, _, _ in schemas.FLAT_COLUMNS[source]]
    assert not any(c.startswith("_") for c in struct)


def test_maude_keeps_reports_without_devices():
    sql = B.read_source_sql("maude")
    assert "ARRAY_LENGTH(IFNULL(JSON_QUERY_ARRAY(r.payload, '$.device'), [])) = 0" in sql
    assert "[JSON '{}']" in sql and "WITH OFFSET AS device_seq" in sql


@pytest.mark.parametrize("mutate, match", [
    (lambda s: s.replace("WHERE _ingest_date = @ingest_date", ""), "partition pruning"),
    (lambda s: s.replace("AS root_cause", "AS root_cause_x"), r"\['root_cause'\]"),
    (lambda s: s.replace("`__RAW_TABLE__`", "`x`"), "__RAW_TABLE__"),
])
def test_validator_rejects_bad_sql(mutate, match):
    with pytest.raises(ValueError, match=match):
        B.validate_source_sql("recall", mutate(B.read_source_sql("recall")))


# ── main() with fake BigQuery ────────────────────────────────────────────────

def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["build_staging.py", "--project", "p", "--source", "recall",
                                      "--ingest-date", "2026-10-25", *args])
    B.main()


def query_result(row):
    return types.SimpleNamespace(result=lambda: iter([row]))


def test_empty_raw_partition_runs_no_script(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [query_result(types.SimpleNamespace(n=0))]
    run_main(monkeypatch)
    assert client.query.call_count == 1                      # only the COUNT(*)


def test_staging_script_runs_after_the_count(fake_gcp, monkeypatch, capsys):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [
        query_result(types.SimpleNamespace(n=10)),
        query_result(types.SimpleNamespace(flat_rows=10, null_key_rows=1, staged_rows=8))]
    run_main(monkeypatch)
    script = client.query.call_args_list[1].args[0]
    assert "BEGIN TRANSACTION" in script and "recall_stg" in script
    assert "8 staged (1 null-key rows dropped, 1 duplicates removed)" in capsys.readouterr().out
