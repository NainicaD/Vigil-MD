"""Tests for scripts/warehouse/merge_prod.py (MERGE staging → prod, --rebuild).

The SQL is checked as text. BigQuery is the fake_gcp MagicMock. No GCP.
"""
import re
import sys
import types

import pytest

from scripts.warehouse import merge_prod as M
from scripts.warehouse import schemas

SENTINEL = "DATE '1900-01-01'"


def prod_cols(source):
    spec = next(s for s in schemas.table_specs("p") if s["table_id"] == schemas.prod_table("p", source))
    return [n for n, _, _ in spec["columns"]]


def merge_sql(source):
    return M.render_merge_sql(source, schemas.prod_table("p", source), schemas.staging_table("p", source))


def rebuild_sql(source):
    return M.render_rebuild_sql(source, schemas.prod_table("p", source), schemas.staging_table("p", source))


# ── PRUNE_BY_DATE ────────────────────────────────────────────────────────────

def test_prune_only_date_partitioned_sources():
    assert set(M.PRUNE_BY_DATE) == set(schemas.SOURCES)
    assert M.PRUNE_BY_DATE["gudid"] is False
    assert all(s in schemas.DATE_PARTITIONED_PROD for s, on in M.PRUNE_BY_DATE.items() if on)


# ── MERGE ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", schemas.SOURCES)
def test_on_clause(source):
    on = re.search(r"\nON (.+?)\nWHEN MATCHED", merge_sql(source), re.S).group(1)
    for k in schemas.MERGE_KEYS[source]:
        assert f"T.{k} = S.{k}" in on
    if M.PRUNE_BY_DATE[source]:
        assert "T._part_date BETWEEN @min_d AND @max_d" in on and f"T._part_date = {SENTINEL}" in on
    else:
        assert "_part_date" not in on


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_insert_columns_equal_prod_columns(source):
    m = re.search(r"INSERT \((.+?)\)\n\s+VALUES \((.+?)\);", merge_sql(source), re.S)
    cols, vals = m.group(1).split(", "), m.group(2).split(", ")
    assert sorted(cols) == sorted(prod_cols(source)) and len(cols) == len(vals)


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_update_changes_only_changed_rows_and_keeps_first_loaded_at(source):
    sql = merge_sql(source)
    assert "WHEN MATCHED AND T.record_hash != S.record_hash THEN" in sql
    upd = re.search(r"UPDATE SET (.+?)\nWHEN NOT MATCHED", sql, re.S).group(1)
    assert "first_loaded_at" not in upd and "last_updated_at = CURRENT_TIMESTAMP()" in upd


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_daily_merge_never_deletes(source):
    sql = merge_sql(source)
    assert "NOT MATCHED BY SOURCE" not in sql and "DELETE" not in sql


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_merge_and_duplicate_check_share_one_transaction(source):
    sql = merge_sql(source)
    assert "WHERE _ingest_date = @ingest_date" in sql
    order = ["BEGIN TRANSACTION", "MERGE", "SET dup_keys", "ROLLBACK TRANSACTION",
             "RAISE", "COMMIT TRANSACTION"]
    positions = [sql.index(s) for s in order]
    assert positions == sorted(positions)


# ── rebuild ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", schemas.SOURCES)
def test_rebuild_keeps_the_table(source):
    sql = rebuild_sql(source)
    cols = re.search(r"INSERT INTO `[^`]+` \((.+?)\)", sql).group(1).split(", ")
    assert sorted(cols) == sorted(prod_cols(source))
    assert "_ingest_date >= DATE '1900-01-01'" in sql
    assert f"PARTITION BY {', '.join(schemas.MERGE_KEYS[source])}" in sql
    assert "CREATE OR REPLACE" not in sql
    order = ["BEGIN TRANSACTION", "DELETE FROM", "INSERT INTO", "ROLLBACK TRANSACTION", "COMMIT TRANSACTION"]
    positions = [sql.index(s) for s in order]
    assert positions == sorted(positions)


# ── bounds ───────────────────────────────────────────────────────────────────

def test_bounds_for_gudid_uses_a_typed_null():
    # regression: an untyped NULL is INT64 in BigQuery and failed against a DATE
    sql = M.render_bounds_sql("gudid", schemas.staging_table("p", "gudid"))
    assert "CAST(NULL AS DATE) AS p" in sql


def test_bounds_for_date_sources_skip_the_sentinel():
    sql = M.render_bounds_sql("maude", schemas.staging_table("p", "maude"))
    assert f"COALESCE(record_date, {SENTINEL}) AS p" in sql
    assert f"MIN(IF(p = {SENTINEL}, NULL, p))" in sql


# ── CLI and main() with fake BigQuery ────────────────────────────────────────

def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["merge_prod.py", "--project", "p", *args])
    M.main()


def query_result(row):
    return types.SimpleNamespace(result=lambda: iter([row]))


def test_rebuild_without_confirmation_refuses(fake_gcp, monkeypatch, capsys):
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, "--source", "maude", "--rebuild")
    assert e.value.code == 2 and "REFUSING" in capsys.readouterr().out
    fake_gcp.bigquery.Client.assert_not_called()


def test_ingest_date_required_without_rebuild(fake_gcp, monkeypatch):
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, "--source", "maude")
    assert e.value.code == 2


def test_empty_staging_partition_runs_no_merge(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [query_result(types.SimpleNamespace(n=0, min_d=None, max_d=None))]
    run_main(monkeypatch, "--source", "maude", "--ingest-date", "2026-10-25")
    assert client.query.call_count == 1


def params_passed(fake_gcp):
    return {c.args[0]: c.args[2] for c in fake_gcp.bigquery.ScalarQueryParameter.call_args_list}


def test_merge_gets_the_staging_date_range(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [
        query_result(types.SimpleNamespace(n=3, min_d="2024-01-02", max_d="2024-03-04")),
        query_result(types.SimpleNamespace(rows_inserted_or_updated=3, dup_keys=0))]
    run_main(monkeypatch, "--source", "maude", "--ingest-date", "2026-10-25")
    assert client.query.call_count == 2 and "MERGE" in client.query.call_args_list[1].args[0]
    p = params_passed(fake_gcp)
    assert p["min_d"] == "2024-01-02" and p["max_d"] == "2024-03-04"


def test_all_null_dates_match_only_the_sentinel(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [
        query_result(types.SimpleNamespace(n=2, min_d=None, max_d=None)),
        query_result(types.SimpleNamespace(rows_inserted_or_updated=2, dup_keys=0))]
    run_main(monkeypatch, "--source", "recall", "--ingest-date", "2026-10-25")
    p = params_passed(fake_gcp)
    assert p["min_d"] == p["max_d"] == "1900-01-01"


def test_gudid_merge_has_no_date_params(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [
        query_result(types.SimpleNamespace(n=2, min_d=None, max_d=None)),
        query_result(types.SimpleNamespace(rows_inserted_or_updated=2, dup_keys=0))]
    run_main(monkeypatch, "--source", "gudid", "--ingest-date", "2026-10-25")
    assert "min_d" not in params_passed(fake_gcp)
