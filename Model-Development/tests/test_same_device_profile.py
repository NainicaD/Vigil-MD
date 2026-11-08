"""Tests for the same-device profiling queries and runner. No real GCP: BigQuery is faked."""
import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from vigil_model import bq_utils
from pipeline import same_device_profile

PROJECT = "test-project"
SQL_FILES = sorted(same_device_profile.SQL_DIR.glob("*.sql"))


def test_there_are_queries():
    assert len(SQL_FILES) >= 10


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_every_query_has_a_full_header(path):
    header = bq_utils.load_query_file(path).header
    for key in bq_utils.HEADER_KEYS:
        assert header.get(key), f"{path.name}: missing '-- {key}:' header"


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_every_part_renders_read_only(path):
    qf = bq_utils.load_query_file(path)
    assert qf.parts
    for part, sql in qf.parts:
        final = bq_utils.render(sql, PROJECT)
        assert f"`{PROJECT}.vigil_md_prod." in final, f"{path.name}/{part} reads no prod table"
        bq_utils.assert_read_only(final)


def test_parts_are_split_on_markers(tmp_path):
    p = tmp_path / "99_x.sql"
    p.write_text("-- Title: t\n-- Check: c\n-- Why: w\n-- Informs: i\n"
                 "-- @part a\nSELECT 1 FROM `__MAUDE__`\n-- @part b\nSELECT 2 FROM `__GUDID__`\n")
    qf = bq_utils.load_query_file(p)
    assert [n for n, _ in qf.parts] == ["a", "b"]
    assert qf.parts[1][1] == "SELECT 2 FROM `__GUDID__`"
    assert qf.header["Check"] == "c"


def test_multiline_header_is_joined(tmp_path):
    p = tmp_path / "99_x.sql"
    p.write_text("-- Title: t\n-- Check: first line\n--   second line\nSELECT 1")
    assert bq_utils.load_query_file(p).header["Check"] == "first line second line"


@pytest.mark.parametrize("sql", [
    "DELETE FROM `p.vigil_md_prod.maude` WHERE TRUE",
    "SELECT * FROM `p.vigil_md_staging.maude_stg`",
    "SELECT * FROM `p.vigil_md_prod.maude`; DROP TABLE `p.vigil_md_prod.maude`",
    "CREATE TEMP FUNCTION f() AS (1); SELECT 1 FROM `p.vigil_md_prod.maude`",
])
def test_read_only_guard_rejects(sql):
    with pytest.raises(ValueError):
        bq_utils.assert_read_only(sql)


def test_unknown_placeholder_is_an_error():
    with pytest.raises(ValueError, match="__RAW_TABLE__"):
        bq_utils.render("SELECT 1 FROM `__RAW_TABLE__`", PROJECT)


def _client(bytes_per_query, rows_by_call=None):
    """Fake BigQuery client: dry runs report bytes_per_query; real runs return rows."""
    rows_by_call = list(rows_by_call or [])
    calls = []

    def query(sql, job_config=None):
        calls.append(sql)
        dry = getattr(job_config, "dry_run", False) is True
        rows = [] if dry else (rows_by_call.pop(0) if rows_by_call else [])
        return SimpleNamespace(
            total_bytes_processed=bytes_per_query,
            result=lambda: [SimpleNamespace(items=lambda r=r: r.items()) for r in rows])
    return SimpleNamespace(query=query, calls=calls)


@pytest.fixture
def tiny_sql_dir(tmp_path):
    d = tmp_path / "sql"
    d.mkdir()
    (d / "01_a.sql").write_text(
        "-- Title: A\n-- Check: counts\n-- Why: w\n-- Informs: i\nSELECT COUNT(*) AS n FROM `__MAUDE__`\n")
    (d / "02_b.sql").write_text(
        "-- Title: B\n-- Check: c\n-- Why: w\n-- Informs: i\n"
        "-- @part one\nSELECT 1 AS x FROM `__GUDID__`\n-- @part two\nSELECT 2 AS y FROM `__GUDID__`\n")
    return d


def _job_config_factory(fake_bigquery):
    fake_bigquery.QueryJobConfig.side_effect = lambda **kw: SimpleNamespace(**kw)


def test_over_budget_stops_before_running_anything(fake_bigquery, tiny_sql_dir, tmp_path):
    _job_config_factory(fake_bigquery)
    client = _client(bytes_per_query=int(9e9))
    files = bq_utils.load_query_dir(tiny_sql_dir)
    rc = same_device_profile.run(client, PROJECT, files, tmp_path / "out", max_gb=5.0, sql_dir=tiny_sql_dir)
    assert rc == same_device_profile.EXIT_OVER_BUDGET
    assert len(client.calls) == 3            # three dry runs, no real job
    assert not (tmp_path / "out").exists()


def test_sql_error_is_reported_for_every_part_and_nothing_runs(fake_bigquery, tiny_sql_dir, tmp_path):
    _job_config_factory(fake_bigquery)
    calls = []

    def query(sql, job_config=None):
        calls.append(sql)
        raise RuntimeError("Syntax error")
    files = bq_utils.load_query_dir(tiny_sql_dir)
    rc = same_device_profile.run(SimpleNamespace(query=query), PROJECT, files, tmp_path / "out",
                        max_gb=5.0, sql_dir=tiny_sql_dir)
    assert rc == same_device_profile.EXIT_BAD_SQL
    assert len(calls) == 3                   # every part was tried, none was run for real
    assert not (tmp_path / "out").exists()


def test_estimate_only_runs_no_job(fake_bigquery, tiny_sql_dir, tmp_path):
    _job_config_factory(fake_bigquery)
    client = _client(bytes_per_query=1000)
    files = bq_utils.load_query_dir(tiny_sql_dir)
    rc = same_device_profile.run(client, PROJECT, files, tmp_path / "out", max_gb=5.0,
                        estimate_only=True, sql_dir=tiny_sql_dir)
    assert rc == same_device_profile.EXIT_OK
    assert len(client.calls) == 3
    assert not (tmp_path / "out").exists()


def test_full_run_writes_csvs_and_results_md(fake_bigquery, tiny_sql_dir, tmp_path):
    _job_config_factory(fake_bigquery)
    client = _client(bytes_per_query=1000,
                     rows_by_call=[[{"n": 42}], [{"x": 1}], [{"y": "a|b"}]])
    out = tmp_path / "out"
    files = bq_utils.load_query_dir(tiny_sql_dir)
    rc = same_device_profile.run(client, PROJECT, files, out, max_gb=5.0, sql_dir=tiny_sql_dir)
    assert rc == same_device_profile.EXIT_OK
    assert sorted(p.name for p in out.glob("*.csv")) == ["01_a.csv", "02_b__one.csv", "02_b__two.csv"]
    with (out / "01_a.csv").open() as f:
        assert list(csv.DictReader(f)) == [{"n": "42"}]
    md = (out / "RESULTS.md").read_text()
    assert "## 01_a: A" in md and "**Check:** counts" in md
    assert "| 42 |" in md
    assert "a\\|b" in md                      # pipes escaped in markdown tables


def test_partial_run_keeps_other_results(fake_bigquery, tiny_sql_dir, tmp_path):
    _job_config_factory(fake_bigquery)
    out = tmp_path / "out"
    files = bq_utils.load_query_dir(tiny_sql_dir)
    same_device_profile.run(_client(1000, [[{"n": 1}], [{"x": 1}], [{"y": 2}]]), PROJECT, files, out,
                   max_gb=5.0, sql_dir=tiny_sql_dir)
    only_a = bq_utils.load_query_dir(tiny_sql_dir, only=["01"])
    same_device_profile.run(_client(1000, [[{"n": 7}]]), PROJECT, only_a, out, max_gb=5.0,
                   sql_dir=tiny_sql_dir)
    md = (out / "RESULTS.md").read_text()
    assert "| 7 |" in md and "## 02_b: B" in md and "| 2 |" in md


def test_print_only_makes_no_gcp_call(capsys):
    assert same_device_profile.main(["--project", PROJECT, "--print-only", "--only", "01"]) == 0
    assert f"`{PROJECT}.vigil_md_prod.maude`" in capsys.readouterr().out
