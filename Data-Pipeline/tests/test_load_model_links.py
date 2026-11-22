"""Tests for scripts/warehouse/load_model_links.py (the matching model's links and their ELK record).

SQL is checked as text; BigQuery is a fake client and the logger is captured (no GCP, no Logstash).
"""
import json
import re
import sys
import types

import pytest

from scripts.warehouse import load_model_links as M
from scripts.warehouse import schemas

P = "p"
COUNTS = dict(links=120, duplicates=0, not_model=0, bad_score=0)
REPORT = {"n_pairs": 500, "n_match": 120, "rate_match": 0.24, "score_hist": [1] * 10,
          "bundle_version": "v1.0.0", "score_psi": 0.03}


# ── SQL ───────────────────────────────────────────────────────────────────────

def test_default_dataset_is_the_test_extract_not_prod(monkeypatch, capsys):
    for mod in [m for m in sys.modules if m.startswith("google.cloud.bigquery")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)       # import would fail
    assert M.main(["--project", P, "--print-only"]) == 0
    out = capsys.readouterr().out
    assert "`p.vigil_md_dev.link_maude_gudid_model`" in out and "vigil_md_prod" not in out


def test_only_prod_or_a_dev_extract_can_be_written():
    assert M.target(P, "vigil_md_prod") == "p.vigil_md_prod.link_maude_gudid_model"
    for ds in (schemas.DATASETS["raw"], schemas.DATASETS["staging"], schemas.DATASETS["ops"], "vigil_md_model"):
        with pytest.raises(ValueError):
            M.merge_sql(P, ds, full=False)
    assert M.main(["--project", P, "--dataset", "vigil_md_raw", "--print-only"]) == M.EXIT_BAD_INPUT


def test_create_has_the_link_columns_and_never_replaces():
    sql = M.create_sql(P, "vigil_md_dev")
    assert sql.startswith("CREATE TABLE IF NOT EXISTS `p.vigil_md_dev.link_maude_gudid_model`")
    assert re.findall(r"^  (\w+) ", sql, re.M) == [c for c, _ in schemas.LINK_COLUMNS]


def test_incremental_run_deletes_only_the_rescored_maude_rows():
    sql = M.merge_sql(P, "vigil_md_dev", full=False)
    assert ("DELETE FROM `p.vigil_md_dev.link_maude_gudid_model` WHERE left_id IN "
            "(SELECT maude_id FROM `p.vigil_md_model.match_candidates_vigil_md_dev`)") in sql
    assert "FROM `p.vigil_md_staging.link_maude_gudid_model_load_vigil_md_dev`" in sql


def test_full_run_deletes_every_model_link():
    assert "DELETE FROM `p.vigil_md_dev.link_maude_gudid_model` WHERE TRUE;" in M.merge_sql(P, "vigil_md_dev", full=True)


def test_duplicate_pairs_roll_back_inside_the_transaction():
    sql = M.merge_sql(P, "vigil_md_dev", full=False)
    assert sql.index("BEGIN TRANSACTION") < sql.index("DELETE") < sql.index("INSERT") < sql.index("ROLLBACK")
    assert "IF dup_keys > 0 THEN\n  ROLLBACK TRANSACTION;\n  RAISE" in sql
    assert sql.index("END IF") < sql.index("COMMIT TRANSACTION")


@pytest.mark.parametrize("bad,problem", [
    ({}, None),
    ({"duplicates": 2}, "duplicate"),
    ({"not_model": 1}, "method"),
    ({"bad_score": 3}, "score outside"),
])
def test_check_problems(bad, problem):
    found = M.check_problems({**COUNTS, **bad})
    assert (problem is None and not found) or (problem and problem in " ".join(found))


def test_run_metrics_join_the_report_and_the_link_counts():
    m = M.run_metrics(REPORT, COUNTS, {"n_deleted": 7, "n_inserted": 9}, "vigil_md_dev", False, "manual__1")
    assert m["rate_match"] == 0.24 and m["score_psi"] == 0.03 and m["bundle_version"] == "v1.0.0"
    assert (m["links_deleted"], m["links_inserted"], m["links_total"]) == (7, 9, 120)
    assert (m["dataset"], m["full"], m["run_id"]) == ("vigil_md_dev", False, "manual__1")


# ── main (fake BigQuery, captured logger) ─────────────────────────────────────

class FakeClient:
    def __init__(self, counts=None, merge_error=None):
        self.counts = {**COUNTS, **(counts or {})}
        self.merge_error = merge_error
        self.sql, self.loads, self.caps = [], [], []

    def query(self, sql, job_config=None):
        self.sql.append(sql.split("\n")[0])
        if job_config is not None:
            self.caps.append(job_config.maximum_bytes_billed)
        if sql.startswith("DECLARE"):
            def result():
                if self.merge_error:
                    raise RuntimeError(self.merge_error)
                return iter([{"n_deleted": 7, "n_inserted": 9}])
            return types.SimpleNamespace(result=result)
        if sql.startswith("SELECT\n  COUNT(*) AS links"):
            return types.SimpleNamespace(result=lambda: iter([self.counts]))
        return types.SimpleNamespace(result=lambda: None)      # CREATE TABLE, CREATE VIEW

    def load_table_from_file(self, f, table, job_config=None):
        self.loads.append((table, job_config))
        return types.SimpleNamespace(result=lambda: None)


@pytest.fixture
def env(monkeypatch, tmp_path):
    holder, records = {}, []

    def make(**kw):
        holder["c"] = FakeClient(**kw)
        return holder["c"]
    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", lambda project: holder["c"])
    monkeypatch.setattr(M.log, "info", lambda msg, *a, extra=None, **kw: records.append((msg, extra)))
    links, report = tmp_path / "links.parquet", tmp_path / "report.json"
    links.write_bytes(b"PAR1")
    report.write_text(json.dumps(REPORT))
    return types.SimpleNamespace(make=make, records=records, links=links, report=report)


def run(env, *extra):
    return M.main(["--project", P, "--links", str(env.links), "--report", str(env.report),
                   "--run-id", "manual__1", *extra])


def test_load_creates_loads_merges_checks_then_rebuilds_the_view(env):
    c = env.make()
    assert run(env) == 0
    assert c.sql == ["CREATE TABLE IF NOT EXISTS `p.vigil_md_dev.link_maude_gudid_model` (",
                     "DECLARE dup_keys INT64;", "SELECT",
                     "CREATE OR REPLACE VIEW `p.vigil_md_dev.links` AS"]
    (table, cfg), = c.loads
    assert table == "p.vigil_md_staging.link_maude_gudid_model_load_vigil_md_dev"
    assert c.caps and all(0 < cap <= 5e9 for cap in c.caps)


def test_view_includes_the_exact_and_the_model_links():
    sql = M.view_sql(P, "vigil_md_dev", M.ORDER + [M.TABLE])
    assert "FROM `p.vigil_md_dev.link_enforcement_recall`" in sql
    assert "FROM `p.vigil_md_dev.link_maude_gudid_model`" in sql


def test_one_elk_record_with_the_run_metrics(env):
    env.make()
    run(env)
    (msg, extra), = [r for r in env.records if r[0] == "matching_run_metrics"]
    assert extra["n_pairs"] == 500 and extra["links_inserted"] == 9 and extra["links_total"] == 120
    assert extra["run_id"] == "manual__1" and extra["dataset"] == "vigil_md_dev"


def test_duplicates_in_the_transaction_exit_3_without_a_view(env):
    c = env.make(merge_error="p.vigil_md_dev.link_maude_gudid_model: 2 duplicate (left_id, right_id) pairs")
    assert run(env) == M.EXIT_CHECK_FAILED
    assert not any(s.startswith("CREATE OR REPLACE VIEW") for s in c.sql)
    assert env.records == []


def test_other_errors_are_not_hidden(env):
    env.make(merge_error="Access Denied")
    with pytest.raises(RuntimeError, match="Access Denied"):
        run(env)


def test_a_failed_check_exits_3_before_the_view(env):
    c = env.make(counts={"bad_score": 1})
    assert run(env) == M.EXIT_CHECK_FAILED
    assert not any(s.startswith("CREATE OR REPLACE VIEW") for s in c.sql)


def test_missing_links_file_is_bad_input(env, tmp_path):
    env.make()
    assert M.main(["--project", P, "--links", str(tmp_path / "nope.parquet")]) == M.EXIT_BAD_INPUT
