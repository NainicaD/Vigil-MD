"""Tests for query_layer: SQL safety, parameters, the cost guard, and result shaping.

BigQuery is a fake client (no network, no credentials, no cost). Which query a call is for
is recognised by a fixed fragment of its SQL.
"""
import datetime as dt
import types

import pytest
from google.cloud import bigquery

from query_layer import queries, summary
from query_layer.backend import BigQueryBackend, Budget, QueryBudgetExceeded
from query_layer.estimate import plan
from query_layer.terms import NO_HARM, NO_INFORMATION

INJECTION = "x'; DROP TABLE maude; --"
FRAGMENT = {                       # SQL fragment → query name
    "EXTRACT(YEAR FROM record_date)": "counts",
    "ranked AS": "problems",
    "COUNT(*) OVER () AS total_recalls": "recalls",
    "PARTITION BY event_type ORDER BY record_date DESC": "narrative_candidates",
    "LENGTH(t.text) AS text_length": "narrative_texts",
    "ARRAY_TO_STRING(g.product_codes": "identity",
}


def name_of(sql):
    return next(n for frag, n in FRAGMENT.items() if frag in sql)


class FakeClient:
    """Dry runs return estimates[name] bytes; real runs return rows[name] and bill billed[name]."""
    def __init__(self, estimates=None, rows=None, billed=None):
        self.estimates, self.rows, self.billed = estimates or {}, rows or {}, billed or {}
        self.calls = []          # (name, job_config)

    def query(self, sql, job_config):
        name = name_of(sql)
        self.calls.append((name, job_config))
        if job_config.dry_run:
            return types.SimpleNamespace(total_bytes_processed=self.estimates.get(name, 1_000_000))
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


def all_queries(key_type="product_code", key="LZG"):
    qs = [queries.counts(T, key_type, key), queries.problems(T, key_type, key),
          queries.narrative_candidates(T, key_type, key), queries.identity(T, key_type, key),
          queries.narrative_texts(T, ["1"], ["2026-09-30"])]
    if key_type in queries.RECALL_FILTER:
        qs.append(queries.recalls(T, key_type, key))
    return qs


# ── SQL safety ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key_type", ["product_code", "brand", "manufacturer", "udi_di"])
def test_every_query_is_read_only_and_binds_the_key(key_type):
    for q in all_queries(key_type, INJECTION):
        queries.assert_read_only(q.sql)
        assert INJECTION not in q.sql and "DROP TABLE" not in q.sql
        if "key" in q.params:
            assert q.params["key"] == ("STRING", INJECTION)


@pytest.mark.parametrize("sql", [
    "DELETE FROM t WHERE TRUE", "SELECT 1; DROP TABLE t", "UPDATE t SET a = 1",
    "WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", "MERGE t USING s ON TRUE",
    "CREATE TABLE t AS SELECT 1", "EXPORT DATA OPTIONS() AS SELECT 1"])
def test_read_only_check_refuses_writes(sql):
    with pytest.raises(ValueError):
        queries.assert_read_only(sql)


def test_read_only_check_allows_column_names_that_contain_write_words():
    queries.assert_read_only("SELECT last_updated_at, 'drop it' AS note FROM t -- update later")


@pytest.mark.parametrize("filters", [queries.MAUDE_FILTER, queries.RECALL_FILTER,
                                     queries.GUDID_FILTER])
def test_device_filters_use_the_bound_key_only(filters):
    for key_type, condition in filters.items():
        assert "@key" in condition and "'" not in condition and "{" not in condition, key_type


def test_backend_refuses_a_write_statement_before_calling_bigquery():
    client = FakeClient()
    bad = queries.Query("counts", "DELETE FROM t WHERE TRUE -- EXTRACT(YEAR FROM record_date)")
    with pytest.raises(ValueError):
        backend(client).run([bad], Budget())
    assert client.calls == []


def test_unknown_key_type_and_empty_key_are_refused():
    with pytest.raises(ValueError):
        queries.counts(T, "model_number", "780G")
    with pytest.raises(ValueError):
        queries.counts(T, "brand", "  ")
    with pytest.raises(ValueError):                      # no UDI in the recall data
        queries.recalls(T, "udi_di", "00643169007222")


def test_tables_come_from_the_configured_dataset():
    q = queries.problems(backend(FakeClient(), "vigil_md_dev").table, "brand", "X")
    assert "`p.vigil_md_dev.maude`" in q.sql and "`p.vigil_md_dev.maude_problems`" in q.sql
    assert "vigil_md_prod" not in q.sql


def test_problems_leaves_out_no_information_and_no_harm_terms():
    q = queries.problems(T, "product_code", "LZG", top_n=7)
    assert q.params["no_information"] == ("ARRAY<STRING>", list(NO_INFORMATION))
    assert q.params["no_harm"] == ("ARRAY<STRING>", list(NO_HARM))
    assert q.params["top_n"] == ("INT64", 7)
    assert "NOT IN UNNEST(@no_harm)" in q.sql and "'no_harm' AS problem_type" in q.sql


def test_reports_are_counted_once_not_per_device():
    for q in (queries.counts(T, "brand", "X"), queries.narrative_candidates(T, "brand", "X")):
        assert "GROUP BY m.source_record_id" in q.sql
    assert "SELECT DISTINCT m.source_record_id" in queries.problems(T, "brand", "X").sql


def test_narrative_texts_prune_by_month_and_report():
    q = queries.narrative_texts(T, ["11", "12"], ["2026-09-30"], max_chars=300, min_chars=40)
    assert "t._part_date IN UNNEST(@dates)" in q.sql and "IN UNNEST(@report_keys)" in q.sql
    assert q.params["dates"] == ("ARRAY<DATE>", ["2026-09-30"])
    assert q.params["max_chars"] == ("INT64", 300) and q.params["min_chars"] == ("INT64", 40)


def test_parameters_become_bigquery_parameters():
    q = queries.narrative_texts(T, ["1"], ["2026-09-30"])
    params = {p.name: p for p in BigQueryBackend._bq_params(q)}
    assert isinstance(params["dates"], bigquery.ArrayQueryParameter)
    assert params["dates"].array_type == "DATE"
    assert isinstance(params["max_chars"], bigquery.ScalarQueryParameter)


# ── cost guard ────────────────────────────────────────────────────────────────

def test_over_budget_runs_nothing():
    client = FakeClient(estimates={"counts": 1.5e9, "problems": 1.0e9})
    with pytest.raises(QueryBudgetExceeded, match="2.50 GB"):
        backend(client).run([queries.counts(T, "brand", "X"), queries.problems(T, "brand", "X")],
                            Budget(max_gb=2.0))
    assert client.real_runs() == []                     # only dry runs happened


def test_under_budget_runs_with_maximum_bytes_billed_and_tracks_spend():
    client = FakeClient(estimates={"counts": 0.4e9}, billed={"counts": 300_000_000},
                        rows={"counts": [{"year": 2025, "event_type": "Injury", "reports": 3}]})
    budget = Budget(max_gb=1.0)
    (rows,), (rec,) = backend(client).run([queries.counts(T, "brand", "X")], budget)
    (_, cfg), = client.real_runs()
    assert cfg.maximum_bytes_billed == 1_000_000_000
    assert rows == [{"year": 2025, "event_type": "Injury", "reports": 3}]
    assert budget.spent_gb == pytest.approx(0.3) and rec.billed_bytes == 300_000_000
    assert rec.params == {"key": "X"} and rec.estimated_bytes == 400_000_000


def test_budget_is_shared_across_steps_of_one_question():
    client = FakeClient(estimates={"counts": 0.8e9, "identity": 0.8e9},
                        billed={"counts": 800_000_000})
    b, budget = backend(client), Budget(max_gb=1.0)
    b.run([queries.counts(T, "brand", "X")], budget)
    with pytest.raises(QueryBudgetExceeded):
        b.run([queries.identity(T, "brand", "X")], budget)


def test_dates_come_back_as_iso_strings():
    client = FakeClient(rows={"identity": [{"device_name": "X", "d": dt.date(2026, 9, 30)}]})
    (rows,), _ = backend(client).run([queries.identity(T, "brand", "X")], Budget())
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
    ("word " * 200, 1000, ("word " * 120).rstrip() + "…"),       # cut after the last whole word
    ("abc " * 100 + "x" * 600, 1000, ("abc " * 100).rstrip() + "…"),
])
def test_excerpt(text, length, expected):
    assert summary.excerpt(text, length, 600) == expected


def test_excerpt_never_exceeds_the_limit():
    out = summary.excerpt("x" * 900, 900, 600)
    assert len(out) <= 601 and out.endswith("…")


def text_row(key, et, date, text_key="1", text="t" * 80):
    return {"mdr_report_key": key, "mdr_text_key": text_key, "event_type": et,
            "date_received": date, "text_length": len(text), "text": text}


def test_pick_narratives_takes_event_types_in_turn_newest_first():
    rows = [text_row("1", "Malfunction", "2026-09-01"), text_row("2", "Malfunction", "2026-09-05"),
            text_row("3", "Injury", "2026-08-01"), text_row("4", "Death", "2026-01-01"),
            text_row("5", "Injury", "2026-09-09"), text_row("5", "Injury", "2026-09-09", "2")]
    picked = summary.pick_narratives(rows, n=4)
    assert [r["mdr_report_key"] for r in picked] == ["4", "5", "2", "3"]   # Death, Injury, Malf., Injury
    assert len({r["mdr_report_key"] for r in picked}) == 4                  # one text per report


def test_pick_narratives_is_deterministic():
    rows = [text_row(str(i), "Injury", "2026-09-01") for i in range(10)]
    assert summary.pick_narratives(rows, 3) == summary.pick_narratives(list(reversed(rows)), 3)


# ── end to end on the fake client ────────────────────────────────────────────

def test_device_summary():
    client = FakeClient(rows={
        "counts": [{"year": 2025, "event_type": "Death", "reports": 1},
                   {"year": 2025, "event_type": "Malfunction", "reports": 4}],
        "problems": [{"problem_type": "device", "problem_term": "Battery Problem", "reports": 3},
                     {"problem_type": "no_harm", "problem_term": None, "reports": 2}],
        "recalls": [{"recall_number": "Z-1", "recall_class": "Class I", "total_recalls": 1}]})
    out = summary.device_summary(backend(client), "brand", "MiniMed 780G", Budget())
    assert out["counts"]["total_reports"] == 5
    assert out["problems"]["device"] == [{"term": "Battery Problem", "reports": 3}]
    assert out["recalls"]["total"] == 1 and out["recalls"]["shown"][0]["recall_number"] == "Z-1"
    assert "total_recalls" not in out["recalls"]["shown"][0]
    assert [s["query"] for s in out["sources"]] == ["counts", "problems", "recalls"]
    assert set(out["data_as_of"]) == {"maude", "maude_problems", "recall", "enforcement"}


def test_device_summary_by_udi_skips_recalls_with_a_note():
    client = FakeClient()
    out = summary.device_summary(backend(client), "udi_di", "00643169007222", Budget())
    assert "recalls" not in [n for n, _ in client.real_runs()]
    assert out["recalls"]["total"] == 0 and "UDI" in out["recalls"]["note"]


def test_narratives_reads_texts_only_for_the_candidate_reports():
    client = FakeClient(rows={
        "narrative_candidates": [{"mdr_report_key": "7", "event_type": "Injury",
                                  "record_date": "2026-09-30"},
                                 {"mdr_report_key": "8", "event_type": "Death",
                                  "record_date": "2026-08-02"}],
        "narrative_texts": [text_row("7", "Injury", "2026-09-30", text="a " * 400),
                            text_row("8", "Death", "2026-08-02")]})
    out = summary.narratives(backend(client), "brand", "X", n=5, max_chars=100, budget=Budget())
    (_, cfg), = [c for c in client.real_runs() if c[0] == "narrative_texts"]
    params = {p.name: p for p in cfg.query_parameters}
    assert sorted(params["report_keys"].values) == ["7", "8"]
    assert [str(d) for d in params["dates"].values] == ["2026-08-02", "2026-09-30"]
    assert [e["event_type"] for e in out["excerpts"]] == ["Death", "Injury"]
    assert out["excerpts"][1]["shortened"] and out["excerpts"][1]["text"].endswith("…")


def test_narratives_without_candidates_runs_no_text_query():
    client = FakeClient()
    out = summary.narratives(backend(client), "brand", "X", budget=Budget())
    assert out["excerpts"] == []
    assert [n for n, _ in client.real_runs()] == ["narrative_candidates"]


def test_identity():
    client = FakeClient(rows={"identity": [{"manufacturer": "Medtronic", "records": 3}]})
    out = summary.identity(backend(client), "udi_di", "00643169007222", Budget())
    assert out["devices"] == [{"manufacturer": "Medtronic", "records": 3}]


def test_estimate_plan_covers_every_query():
    names = [q.name for q in plan(backend(FakeClient()), "brand", "X", months=2)]
    assert names == ["counts", "problems", "recalls", "narrative_candidates",
                     "narrative_texts", "identity"]
    assert "recalls" not in [q.name for q in plan(backend(FakeClient()), "udi_di", "1", 2)]
