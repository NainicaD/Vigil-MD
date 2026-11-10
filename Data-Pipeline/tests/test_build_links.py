"""Tests for scripts/warehouse/build_links.py (the exact link tables and the links view).

SQL is checked as text; BigQuery is a fake client (no GCP).
"""
import datetime as dt
import re
import sys
import types

import pytest

from scripts.warehouse import build_links as L
from scripts.warehouse import schemas

P, DS = "p", "vigil_md_prod"
T0 = dt.datetime(2026, 11, 10, 6, 0, tzinfo=dt.timezone.utc)
T1 = T0 + dt.timedelta(hours=1)


# ── SQL ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", L.ORDER)
def test_select_has_exactly_the_link_columns_in_order(name):
    sql = L.select_sql(name, P, DS)
    aliases = re.findall(r"\bAS (\w+)(?:,|\n)", sql.split("FROM")[0])
    assert aliases == [c for c, _ in schemas.LINK_COLUMNS]


def test_enforcement_recall_joins_on_the_recall_number_only():
    sql = L.select_sql("link_enforcement_recall", P, DS)
    assert "JOIN `p.vigil_md_prod.recall` AS r ON r.link_key = e.link_key" in sql
    assert "WHERE e.link_key IS NOT NULL" in sql
    assert "'exact_id' AS method" in sql and "CAST(NULL AS FLOAT64) AS score" in sql
    # the event match is a second check, never a join condition
    assert "same_event" in sql and "res_event_number" not in sql.split("JOIN")[1]


def test_create_writes_only_its_link_table_in_the_dataset():
    sql = L.create_sql("link_enforcement_recall", P, "vigil_md_dev")
    assert sql.startswith("CREATE OR REPLACE TABLE `p.vigil_md_dev.link_enforcement_recall`")
    assert re.findall(r"TABLE `([^`]+)`", sql) == ["p.vigil_md_dev.link_enforcement_recall"]
    assert "DELETE" not in sql and "INSERT" not in sql and "MERGE" not in sql


def test_unknown_or_protected_targets_are_refused():
    with pytest.raises(ValueError):
        L.create_sql("enforcement", P, DS)                 # a source table, not a link table
    for ds in (schemas.DATASETS["raw"], schemas.DATASETS["staging"], schemas.DATASETS["ops"]):
        with pytest.raises(ValueError, match="only vigil_md_prod or vigil_md_dev"):
            L.create_sql("link_enforcement_recall", P, ds)
        with pytest.raises(ValueError):
            L.view_sql(P, ds)
    with pytest.raises(ValueError):
        L.select_sql("link_maude_gudid", P, DS)            # not built by the pipeline


def test_view_shows_every_link_table_with_the_same_columns():
    sql = L.view_sql(P, DS)
    assert sql.startswith("CREATE OR REPLACE VIEW `p.vigil_md_prod.links` AS")
    cols = ", ".join(c for c, _ in schemas.LINK_COLUMNS)
    for name in schemas.LINK_TABLES:
        assert f"SELECT {cols} FROM `p.vigil_md_prod.{name}`" in sql
    assert sql.count("SELECT") == len(schemas.LINK_TABLES)


def test_check_counts_both_sides_and_duplicates():
    sql = L.check_sql("link_enforcement_recall", P, DS)
    for col in ("links", "duplicates", "left_rows", "right_rows", "left_unlinked",
                "right_unlinked", "other_event"):
        assert f"AS {col}" in sql
    assert "`p.vigil_md_prod.enforcement`" in sql and "`p.vigil_md_prod.recall`" in sql


@pytest.mark.parametrize("counts,problem", [
    (dict(links=10, duplicates=0, left_rows=10, right_rows=12), None),
    (dict(links=10, duplicates=2, left_rows=10, right_rows=12), "duplicate"),
    (dict(links=0, duplicates=0, left_rows=10, right_rows=12), "no links"),
    (dict(links=0, duplicates=0, left_rows=0, right_rows=0), None),   # empty dev source: fine
])
def test_check_problems(counts, problem):
    found = L.check_problems(counts)
    assert (problem is None and not found) or (problem and problem in " ".join(found))


@pytest.mark.parametrize("sources,links,expected", [
    ({"enforcement": T0, "recall": T0}, {"link_enforcement_recall": T1}, []),
    ({"enforcement": T1, "recall": T0}, {"link_enforcement_recall": T0}, ["link_enforcement_recall"]),
    ({"enforcement": T0, "recall": T1}, {"link_enforcement_recall": T0}, ["link_enforcement_recall"]),
    ({"enforcement": T0, "recall": T0}, {"link_enforcement_recall": None}, ["link_enforcement_recall"]),
])
def test_only_link_tables_older_than_a_source_are_rebuilt(sources, links, expected):
    assert L.tables_to_build(sources, links) == expected


# ── main (fake BigQuery) ──────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, modified=None, dry_gb=0.002, counts=None):
        self.modified = modified or {}
        self.dry_gb = dry_gb
        self.counts = counts or dict(links=40447, duplicates=0, left_rows=40449, right_rows=59703,
                                     left_unlinked=2, right_unlinked=19256, other_event=0)
        self.created, self.views, self.caps = [], [], []

    def get_table(self, table_id):
        from google.api_core.exceptions import NotFound
        name = table_id.rsplit(".", 1)[-1]
        if self.modified.get(name) is None:
            raise NotFound(name)
        return types.SimpleNamespace(modified=self.modified[name])

    def query(self, sql, job_config=None):
        if job_config is not None and job_config.dry_run:
            return types.SimpleNamespace(total_bytes_processed=int(self.dry_gb * 1e9))
        if sql.startswith("CREATE OR REPLACE VIEW"):
            self.views.append(sql)
            return types.SimpleNamespace(result=lambda: None)
        if sql.startswith("CREATE OR REPLACE TABLE"):
            self.created.append(re.search(r"TABLE `[^`]*\.(\w+)`", sql).group(1))
            self.caps.append(job_config.maximum_bytes_billed)
            return types.SimpleNamespace(result=lambda: None, total_bytes_billed=int(self.dry_gb * 1e9))
        assert sql.startswith("SELECT\n  (SELECT COUNT(*)"), sql[:60]          # the check
        return types.SimpleNamespace(result=lambda: iter([self.counts]))


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def make(**kw):
        c = FakeClient(**kw)
        holder["c"] = c
        return c
    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", lambda project: holder["c"])
    return make


def run(*extra):
    return L.main(["--project", P, *extra])


def test_build_creates_the_table_then_the_view(client, capsys):
    c = client()
    assert run() == 0
    assert c.created == ["link_enforcement_recall"] and len(c.views) == 1
    out = capsys.readouterr().out
    assert "40,447 links" in out and "19,256 recall" in out and "view links updated" in out


def test_if_changed_skips_when_links_are_newer(client, capsys):
    c = client(modified={"enforcement": T0, "recall": T0, "link_enforcement_recall": T1})
    assert run("--if-changed") == 0
    assert c.created == [] and c.views == []
    assert "nothing to build" in capsys.readouterr().out


def test_if_changed_builds_when_recall_is_newer(client):
    c = client(modified={"enforcement": T0, "recall": T1, "link_enforcement_recall": T0})
    assert run("--if-changed") == 0 and c.created == ["link_enforcement_recall"]


def test_stops_before_passing_the_budget(client):
    c = client(dry_gb=2.0)
    assert run("--max-gb", "1") == L.EXIT_OVER_BUDGET
    assert c.created == [] and c.views == []


def test_every_job_has_a_billing_cap(client):
    c = client()
    run("--max-gb", "0.5")
    assert c.caps and all(0 < cap <= 0.5e9 for cap in c.caps)


def test_a_failed_check_stops_before_the_view(client):
    c = client(counts=dict(links=10, duplicates=3, left_rows=10, right_rows=12,
                           left_unlinked=0, right_unlinked=2, other_event=0))
    assert run() == L.EXIT_CHECK_FAILED
    assert c.views == []


def test_estimate_only_creates_nothing(client, capsys):
    c = client()
    assert run("--estimate-only") == 0
    assert c.created == [] and c.views == []
    assert "link_enforcement_recall" in capsys.readouterr().out


def test_print_only_makes_no_gcp_call(monkeypatch, capsys):
    for mod in [m for m in sys.modules if m.startswith("google.cloud.bigquery")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", None)       # import would fail
    assert run("--print-only") == 0
    out = capsys.readouterr().out
    assert "CREATE OR REPLACE TABLE" in out and "CREATE OR REPLACE VIEW" in out


def test_link_columns_and_tables_are_declared_once():
    names = [c for c, _ in schemas.LINK_COLUMNS]
    assert len(names) == len(set(names))
    assert schemas.link_table(P, "links") == "p.vigil_md_prod.links"
    with pytest.raises(ValueError):
        schemas.link_table(P, "maude")
