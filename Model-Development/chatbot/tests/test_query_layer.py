"""Tests for query_layer on the summary tables: SQL safety, partition lookups, the cost guard,
and result shaping.

BigQuery is a fake client (no network, no credentials, no cost). Which query a call is for
is recognised by the table it reads.
"""
import datetime as dt
import re
import types

import pytest
from google.cloud import bigquery

from query_layer import config, queries, summary
from query_layer.backend import BigQueryBackend, Budget, QueryBudgetExceeded
from query_layer.estimate import plan

INJECTION = "x'; DROP TABLE maude; --"
TABLE_TO_QUERY = {"entity_catalog": "catalog", "entity_counts": "counts",
                  "entity_problems": "problems", "entity_recalls": "recalls",
                  "entity_reports": "narrative_candidates", "maude_text": "narrative_texts",
                  "device_catalog": "device_details"}


def name_of(sql):
    table = re.search(r"FROM `[^`]*\.(\w+)`", sql).group(1)
    return TABLE_TO_QUERY[table]


class FakeClient:
    """Dry runs return estimates[name] bytes; real runs return rows[name] and bill billed[name]."""
    def __init__(self, estimates=None, rows=None, billed=None):
        self.estimates, self.rows, self.billed = estimates or {}, rows or {}, billed or {}
        self.calls = []          # (name, job_config)

    def query(self, sql, job_config):
        name = name_of(sql)
        self.calls.append((name, job_config))
        if job_config.dry_run:
            return types.SimpleNamespace(total_bytes_processed=self.estimates.get(name, 100_000))
        rows = self.rows.get(name, [])
        return types.SimpleNamespace(result=lambda: iter(rows),
                                     total_bytes_billed=self.billed.get(name, 10_485_760))

    def get_table(self, table_id):
        return types.SimpleNamespace(modified=dt.datetime(2026, 11, 7, 6, 0))

    def real_runs(self):
        return [(n, c) for n, c in self.calls if not c.dry_run]


def backend(client, dataset="vigil_md_test"):
    return BigQueryBackend(project="p", dataset=dataset, client=client)


T = backend(FakeClient()).table


def entity_queries(entity_type="device", entity_id="dev:f0cdc15b4d63"):
    qs = [queries.counts(T, entity_type, entity_id), queries.problems(T, entity_type, entity_id),
          queries.recalls(T, entity_type, entity_id), queries.catalog(T, entity_type, entity_id),
          queries.narrative_candidates(T, entity_type, entity_id)]
    if entity_type == "device":
        qs.append(queries.device_details(T, entity_id))
    return qs


# ── buckets and keys ─────────────────────────────────────────────────────────

def test_bucket_matches_bigquery_and_the_pipeline():
    # the same values as Data-Pipeline/tests/test_build_summaries.py, computed by BigQuery
    keys = [("device", "dev:f0cdc15b4d63"), ("product_code", "LZG"), ("manufacturer", "respironics"),
            ("manufacturer", "medtronic minimed"), ("product_code", "ZZZ")]
    assert [config.entity_bucket(t, i) for t, i in keys] == [428, 627, 574, 407, 712]


def test_entity_params():
    assert queries.entity_params("product_code", " LZG ") == {
        "bucket": ("INT64", 627), "entity_type": ("STRING", "product_code"),
        "entity_id": ("STRING", "LZG")}
    with pytest.raises(ValueError):
        queries.entity_params("brand", "DreamStation")       # not an entity type
    with pytest.raises(ValueError):
        queries.entity_params("device", "  ")


# ── SQL safety and partition pruning ──────────────────────────────────────────

@pytest.mark.parametrize("entity_type", config.ENTITY_TYPES)
def test_every_query_is_read_only_binds_the_id_and_reads_one_partition(entity_type):
    for q in entity_queries(entity_type, INJECTION):
        queries.assert_read_only(q.sql)
        assert INJECTION not in q.sql and "DROP TABLE" not in q.sql
        assert "bucket = @bucket" in q.sql, q.name               # partition filter
        assert q.params["bucket"] == ("INT64", config.entity_bucket(entity_type, INJECTION.strip()))
        assert q.params["entity_id"] == ("STRING", INJECTION.strip())


@pytest.mark.parametrize("sql", [
    "DELETE FROM t WHERE TRUE", "SELECT 1; DROP TABLE t", "UPDATE t SET a = 1",
    "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", "MERGE t USING s ON TRUE",
    "CREATE TABLE t AS SELECT 1", "EXPORT DATA OPTIONS() AS SELECT 1"])
def test_read_only_check_refuses_writes(sql):
    with pytest.raises(ValueError):
        queries.assert_read_only(sql)


def test_read_only_check_allows_column_names_that_contain_write_words():
    queries.assert_read_only("SELECT last_updated_at, 'drop it' AS note FROM t -- update later")


def test_backend_refuses_a_write_statement_before_calling_bigquery():
    client = FakeClient()
    bad = queries.Query("counts", "DELETE FROM `p.d.entity_counts` WHERE TRUE")
    with pytest.raises(ValueError):
        backend(client).run([bad], Budget())
    assert client.calls == []


def test_tables_come_from_the_configured_dataset():
    q = queries.problems(backend(FakeClient(), "vigil_md_dev").table, "product_code", "LZG")
    assert "`p.vigil_md_dev.entity_problems`" in q.sql and "vigil_md_prod" not in q.sql


def test_problems_keep_the_no_harm_row_and_the_top_n():
    q = queries.problems(T, "product_code", "LZG", top_n=7)
    assert "problem_type = 'no_harm' OR rank <= @top_n" in q.sql and q.params["top_n"] == ("INT64", 7)


def test_device_details_reads_device_catalog_by_bucket():
    q = queries.device_details(T, "dev:f0cdc15b4d63")
    assert "bucket = @bucket AND device_id = @entity_id" in q.sql
    assert q.params == {"bucket": ("INT64", 428), "entity_id": ("STRING", "dev:f0cdc15b4d63")}


def test_narrative_texts_prune_by_month_and_report():
    q = queries.narrative_texts(T, ["11", "12"], ["2026-09-30"], max_chars=300, min_chars=40)
    assert "t._part_date IN UNNEST(@dates)" in q.sql and "IN UNNEST(@report_keys)" in q.sql
    assert q.params["dates"] == ("ARRAY<DATE>", ["2026-09-30"])
    assert q.params["max_chars"] == ("INT64", 300) and q.params["min_chars"] == ("INT64", 40)


def test_parameters_become_bigquery_parameters():
    q = queries.narrative_candidates(T, "device", "dev:1")
    params = {p.name: p for p in BigQueryBackend._bq_params(q)}
    assert isinstance(params["event_types"], bigquery.ArrayQueryParameter)
    assert params["bucket"].type_ == "INT64" and params["entity_id"].value == "dev:1"


# ── cost guard ────────────────────────────────────────────────────────────────

def test_default_cap_is_half_a_gigabyte():
    assert config.MAX_GB_PER_QUESTION == 0.5


def test_over_budget_runs_nothing():
    client = FakeClient(estimates={"counts": 0.3e9, "problems": 0.3e9})
    with pytest.raises(QueryBudgetExceeded, match="0.60 GB"):
        backend(client).run([queries.counts(T, "device", "dev:1"),
                             queries.problems(T, "device", "dev:1")], Budget(max_gb=0.5))
    assert client.real_runs() == []


def test_under_budget_runs_with_maximum_bytes_billed_and_tracks_spend():
    client = FakeClient(billed={"counts": 10_485_760},
                        rows={"counts": [{"year": 2025, "event_type": "Injury", "reports": 3}]})
    budget = Budget(max_gb=0.5)
    (rows,), (rec,) = backend(client).run([queries.counts(T, "device", "dev:1")], budget)
    (_, cfg), = client.real_runs()
    assert cfg.maximum_bytes_billed == 500_000_000
    assert rows == [{"year": 2025, "event_type": "Injury", "reports": 3}]
    assert budget.spent_gb == pytest.approx(0.0105, abs=1e-4) and rec.params["entity_id"] == "dev:1"


def test_budget_is_shared_across_steps_of_one_question():
    client = FakeClient(estimates={"counts": 0.3e9, "catalog": 0.3e9}, billed={"counts": 300_000_000})
    b, budget = backend(client), Budget(max_gb=0.5)
    b.run([queries.counts(T, "device", "dev:1")], budget)
    with pytest.raises(QueryBudgetExceeded):
        b.run([queries.catalog(T, "device", "dev:1")], budget)


def test_dates_come_back_as_iso_strings():
    client = FakeClient(rows={"catalog": [{"title": "X", "d": dt.date(2026, 9, 30)}]})
    (rows,), _ = backend(client).run([queries.catalog(T, "device", "dev:1")], Budget())
    assert rows[0]["d"] == "2026-09-30"


# ── shaping ───────────────────────────────────────────────────────────────────

def test_summarize_counts():
    rows = [{"year": 2024, "event_type": "Injury", "reports": 3},
            {"year": 2025, "event_type": "Injury", "reports": 2},
            {"year": 2025, "event_type": "Death", "reports": 1},
            {"year": None, "event_type": "(none)", "reports": 4}]
    out = summary.summarize_counts(rows)
    assert out["total_reports"] == 10
    assert out["by_event_type"] == {"Injury": 5, "(none)": 4, "Death": 1}
    assert out["by_year"] == {2024: {"Injury": 3}, 2025: {"Injury": 2, "Death": 1}}


def test_summarize_problems():
    rows = [{"problem_type": "device", "problem_term": "Break", "reports": 2},
            {"problem_type": "device", "problem_term": "Leak", "reports": 5},
            {"problem_type": "patient", "problem_term": "Pain", "reports": 1},
            {"problem_type": "no_harm", "problem_term": None, "reports": 9}]
    out = summary.summarize_problems(rows)
    assert [t["term"] for t in out["device"]] == ["Leak", "Break"]
    assert out["patient"] == [{"term": "Pain", "reports": 1}] and out["no_harm_reports"] == 9


@pytest.mark.parametrize("text, length, expected", [
    ("short text", 10, "short text"),
    ("word " * 200, 1000, ("word " * 120).rstrip() + "…"),
    ("abc " * 100 + "x" * 600, 1000, ("abc " * 100).rstrip() + "…"),
])
def test_excerpt(text, length, expected):
    assert summary.excerpt(text, length, 600) == expected


def text_row(key, et, date, text_key="1", text="t" * 80):
    return {"mdr_report_key": key, "mdr_text_key": text_key, "event_type": et,
            "date_received": date, "text_length": len(text), "text": text}


def test_pick_narratives_takes_event_types_in_turn_newest_first():
    rows = [text_row("1", "Malfunction", "2026-09-01"), text_row("2", "Malfunction", "2026-09-05"),
            text_row("3", "Injury", "2026-08-01"), text_row("4", "Death", "2026-01-01"),
            text_row("5", "Injury", "2026-09-09"), text_row("5", "Injury", "2026-09-09", "2")]
    picked = summary.pick_narratives(rows, n=4)
    assert [r["mdr_report_key"] for r in picked] == ["4", "5", "2", "3"]
    assert len({r["mdr_report_key"] for r in picked}) == 4


# ── end to end on the fake client ────────────────────────────────────────────

def test_device_summary():
    client = FakeClient(rows={
        "catalog": [{"title": "DreamStation", "subtitle": "Respironics, Inc.", "product_codes": ["BZD"]}],
        "counts": [{"year": 2025, "event_type": "Death", "reports": 1},
                   {"year": 2025, "event_type": "Malfunction", "reports": 4}],
        "problems": [{"problem_type": "device", "problem_term": "Battery Problem", "reports": 3},
                     {"problem_type": "no_harm", "problem_term": None, "reports": 2}],
        "recalls": [{"recall_number": "Z-1", "recall_class": "Class I", "total_recalls": 1}]})
    out = summary.device_summary(backend(client), "device", "dev:f0cdc15b4d63", Budget())
    assert out["entity"] == {"type": "device", "id": "dev:f0cdc15b4d63", "found": True,
                             "title": "DreamStation", "subtitle": "Respironics, Inc.",
                             "product_codes": ["BZD"]}
    assert out["counts"]["total_reports"] == 5 and "UDI" in out["counts_note"]
    assert out["problems"]["device"] == [{"term": "Battery Problem", "reports": 3}]
    assert out["recalls"]["total"] == 1 and "total_recalls" not in out["recalls"]["shown"][0]
    assert [s["query"] for s in out["sources"]] == ["catalog", "counts", "problems", "recalls"]
    assert set(out["data_as_of"]) == {"entity_catalog", "entity_counts", "entity_problems",
                                      "entity_recalls"}


def test_unknown_entity_is_reported_not_found():
    out = summary.device_summary(backend(FakeClient()), "product_code", "ZZZ", Budget())
    assert out["entity"]["found"] is False and out["counts"]["total_reports"] == 0


def test_narratives_reads_texts_only_for_the_candidate_reports():
    client = FakeClient(rows={
        "narrative_candidates": [{"mdr_report_key": "7", "event_type": "Injury", "record_date": "2026-09-30"},
                                 {"mdr_report_key": "8", "event_type": "Death", "record_date": "2026-08-02"}],
        "narrative_texts": [text_row("7", "Injury", "2026-09-30", text="a " * 400),
                            text_row("8", "Death", "2026-08-02")]})
    out = summary.narratives(backend(client), "device", "dev:1", n=5, max_chars=100, budget=Budget())
    (_, cfg), = [c for c in client.real_runs() if c[0] == "narrative_texts"]
    params = {p.name: p for p in cfg.query_parameters}
    assert sorted(params["report_keys"].values) == ["7", "8"]
    assert [str(d) for d in params["dates"].values] == ["2026-08-02", "2026-09-30"]
    assert [e["event_type"] for e in out["excerpts"]] == ["Death", "Injury"]
    assert out["excerpts"][1]["shortened"] and out["excerpts"][1]["text"].endswith("…")


def test_narratives_without_candidates_runs_no_text_query():
    client = FakeClient()
    out = summary.narratives(backend(client), "device", "dev:1", budget=Budget())
    assert out["excerpts"] == [] and [n for n, _ in client.real_runs()] == ["narrative_candidates"]


def test_identity_for_a_device_and_for_a_product_code():
    client = FakeClient(rows={"device_details": [{"name": "DreamStation", "company": "Respironics"}],
                              "catalog": [{"title": "Product code LZG", "top_names": ["OMNIPOD"]}]})
    dev = summary.identity(backend(client), "device", "dev:1", Budget())
    assert dev["details"] == {"name": "DreamStation", "company": "Respironics"}
    pc = summary.identity(backend(client), "product_code", "LZG", Budget())
    assert pc["details"]["top_names"] == ["OMNIPOD"]
    assert summary.identity(backend(FakeClient()), "device", "dev:none", Budget())["entity"]["found"] is False


def test_estimate_plan_covers_every_query():
    names = [q.name for q in plan(backend(FakeClient()), "device", "dev:1", months=2)]
    assert names == ["catalog", "counts", "problems", "recalls", "narrative_candidates",
                     "narrative_texts", "device_details"]
    assert "device_details" not in [q.name for q in plan(backend(FakeClient()), "product_code", "LZG", 2)]


# ── parallel runs and remembered table dates (API speed) ──────────────────────

def test_parallel_runs_keep_the_order_and_add_up_the_bill():
    rows = {"counts": [{"year": 2024, "event_type": "Death", "reports": 1}],
            "catalog": [{"entity_type": "device", "entity_id": "dev:f0cdc15b4d63", "title": "X"}]}
    client = FakeClient(rows=rows, billed={"counts": 20_000_000, "catalog": 10_485_760})
    b, budget = backend(client), Budget(max_gb=0.5)
    qs = [queries.counts(b.table, "device", "dev:f0cdc15b4d63"),
          queries.catalog(b.table, "device", "dev:f0cdc15b4d63")]
    (cnt, cat), records = b.run(qs, budget)
    assert cnt == rows["counts"] and cat == rows["catalog"]
    assert [r.name for r in records] == ["counts", "catalog"]
    assert budget.spent_gb == pytest.approx((20_000_000 + 10_485_760) / 1e9)


def test_each_parallel_job_is_capped_so_together_they_fit_the_budget():
    client = FakeClient(estimates={"counts": 100_000_000, "catalog": 50_000_000})
    b, budget = backend(client), Budget(max_gb=0.5)
    qs = [queries.counts(b.table, "device", "dev:f0cdc15b4d63"),
          queries.catalog(b.table, "device", "dev:f0cdc15b4d63")]
    b.run(qs, budget)
    caps = {n: c.maximum_bytes_billed for n, c in client.real_runs()}
    # each job: what is left minus the other job's estimate
    assert caps == {"counts": 500_000_000 - 50_000_000, "catalog": 500_000_000 - 100_000_000}
    est = {"counts": 100_000_000, "catalog": 50_000_000}
    # any one job billing up to its cap, the others their estimates: still within the budget
    for name, cap in caps.items():
        assert cap + sum(v for k, v in est.items() if k != name) <= 500_000_000


def test_table_dates_are_remembered_then_refreshed():
    now = [0.0]

    class Counting(FakeClient):
        def __init__(self):
            super().__init__()
            self.table_calls = 0

        def get_table(self, table_id):
            self.table_calls += 1
            return super().get_table(table_id)

    client = Counting()
    b = BigQueryBackend(project="p", dataset="d", client=client, clock=lambda: now[0])
    first = b.data_as_of(["entity_counts", "entity_catalog"])
    b.data_as_of(["entity_counts", "entity_catalog"])
    assert client.table_calls == 2 and first["entity_counts"].startswith("2026-11-07")
    now[0] = 299.0
    b.data_as_of(["entity_counts"])
    assert client.table_calls == 2                  # still within 5 minutes
    now[0] = 300.0
    b.data_as_of(["entity_counts"])
    assert client.table_calls == 3                  # refreshed
