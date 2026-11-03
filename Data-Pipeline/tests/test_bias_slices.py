"""Tests for scripts/analysis/bias_slices.py (bias slice analysis of PROD).

The SQL is checked as text. BigQuery is fake_gcp plus a small FakeClient that
returns canned rows. No GCP.
"""
import csv
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.analysis import bias_slices as B
from scripts.warehouse import schemas

P = "p"
REPO = Path(__file__).resolve().parents[1]


# ── SQL: read-only, PROD only ────────────────────────────────────────────────

def test_every_query_reads_only_prod():
    qs = B.queries(P)          # raises if any query fails the guard
    assert len(qs) == 4 + len(schemas.SOURCES)
    for _, sql in qs:
        assert "vigil_md_raw" not in sql and "vigil_md_staging" not in sql
        assert "payload" not in sql


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM `{schemas.staging_table(P, 'maude')}`",
    f"SELECT * FROM `{schemas.raw_table(P, 'maude')}`",
    f"MERGE `{schemas.prod_table(P, 'maude')}` USING x ON TRUE",
    f"WITH a AS (SELECT 1) DELETE FROM `{schemas.prod_table(P, 'maude')}` WHERE TRUE",
    f"CREATE TABLE `{schemas.prod_table(P, 'x')}` AS SELECT 1",
])
def test_guard_rejects_non_prod_or_writes(sql):
    with pytest.raises(ValueError):
        B.assert_read_only(sql)


def test_guard_allows_columns_that_contain_keywords():
    B.assert_read_only(f"SELECT last_updated_at, action FROM `{schemas.prod_table(P, 'recall')}`")


# ── SQL: the matching rule is unchanged ──────────────────────────────────────

def test_maude_match_is_exact_against_distinct_gudid_primary_di():
    sql = B.render_maude_link_sql(P)
    assert f"SELECT DISTINCT udi_di FROM `{schemas.prod_table(P, 'gudid')}` WHERE udi_di IS NOT NULL" in sql
    assert "ON t.udi_di = g.udi_di" in sql
    assert "UPPER(t.udi_di" not in sql and "TRIM(t.udi_di" not in sql


def test_maude_length_rule_comes_from_expectations():
    assert B.UDI_LEN == (6, 23)
    assert "NOT BETWEEN 6 AND 23" in B.render_maude_link_sql(P)


def test_shape_query_only_classifies_unmatched_rows():
    sql = B.render_unmatched_shape_sql(P)
    assert "WHERE t.udi_di IS NOT NULL AND g.udi_di IS NULL" in sql
    assert "{" not in sql.replace("\\d{14}", "")      # every placeholder was filled
    assert "longer_than_23" in sql and "shorter_than_6" in sql


def test_link_sql_uses_link_key_both_ways():
    sql = B.render_link_sql(P)
    assert sql.count("ON t.link_key = k.link_key") == 2
    assert "'recall_to_enforcement'" in sql and "'enforcement_to_recall'" in sql


# ── SQL: slices ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", schemas.SOURCES)
def test_slices_include_overall_and_unknown_year(source):
    _, _, slices = B.slice_parts(P, source)
    assert "STRUCT('overall' AS slice_name, 'all' AS slice_value)" in slices
    assert "'unknown'" in slices


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_slice_columns_exist_in_prod(source):
    cols = {c for c, _, _ in schemas.FLAT_COLUMNS[source]}
    assert {col for _, col, _ in B.SLICES[source]} <= cols


def test_top_n_and_tiers():
    _, joins, slices = B.slice_parts(P, "maude")
    assert "<= 25 THEN t.product_code ELSE 'OTHER'" in slices
    assert "'top_10'" in slices and "'11_100'" in slices and "'long_tail'" in slices
    assert "LEFT JOIN rk_maude_manufacturer_name_norm ON" in joins


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_completeness_covers_every_business_column(source):
    sql = B.render_completeness_sql(P, source)
    for col, _, _ in schemas.FLAT_COLUMNS[source]:
        assert f"COUNTIF(p_{col}) AS c_{col}" in sql


def test_completeness_treats_blank_strings_and_empty_arrays_as_missing():
    sql = B.render_completeness_sql(P, "gudid")
    assert "NULLIF(TRIM(t.udi_di), '') IS NOT NULL AS p_udi_di" in sql
    assert "ARRAY_LENGTH(t.product_codes) > 0 AS p_product_codes" in sql
    assert "t.publish_date IS NOT NULL AS p_publish_date" in sql


# ── Statistics ───────────────────────────────────────────────────────────────

def test_wilson():
    lo, hi = B.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4) and hi == pytest.approx(0.7634, abs=1e-4)
    assert B.wilson(0, 10)[0] == 0.0 and B.wilson(10, 10)[1] == 1.0


def _r(slice_name, value, n, k, metric="m"):
    return {"metric": metric, "table": "t", "field": "f", "slice_name": slice_name,
            "slice_value": value, "n": n, "numerator": k}


def test_score_flags_large_gap_only_for_big_cells():
    rows = B.score([_r("overall", "all", 1000, 500), _r("s", "low", 200, 40),
                    _r("s", "tiny", 10, 0), _r("s", "close", 500, 260)])
    by = {r["slice_value"]: r for r in rows}
    assert by["low"]["gap_pp"] == pytest.approx(-30.0)
    assert by["low"]["disparity_flag"] and by["low"]["below_four_fifths"]
    assert by["tiny"]["small_cell"] and not by["tiny"]["disparity_flag"]
    assert not by["close"]["disparity_flag"]
    assert not by["all"]["disparity_flag"] and by["all"]["gap_pp"] == 0


def test_four_fifths_is_not_applied_to_error_rates():
    rows = B.score([_r("overall", "all", 1000, 10, "udi_length_out_of_rule"),
                    _r("s", "clean", 500, 0, "udi_length_out_of_rule")])
    assert not rows[1]["below_four_fifths"]


def test_score_handles_empty_denominator():
    rows = B.score([_r("overall", "all", 100, 50), _r("s", "none", 0, 0)])
    assert rows[1]["rate"] is None and not rows[1]["disparity_flag"]


def test_score_needs_an_overall_row():
    with pytest.raises(ValueError):
        B.score([_r("s", "a", 10, 1)])


def test_maude_rows_split_into_three_metrics():
    m = B.maude_rows([{"slice_name": "overall", "slice_value": "all", "n_rows": 10,
                       "n_udi": 6, "n_matched": 3, "n_bad_length": 1}])
    assert (m["udi_availability"][0]["n"], m["udi_availability"][0]["numerator"]) == (10, 6)
    assert (m["primary_di_match"][0]["n"], m["primary_di_match"][0]["numerator"]) == (6, 3)
    assert (m["link"][0]["n"], m["link"][0]["numerator"]) == (10, 3)


def test_completeness_skips_the_slice_own_column():
    raw = [{"slice_name": "event_type", "slice_value": "Injury", "n_rows": 5,
            **{f"c_{c}": 5 for c, _, _ in schemas.FLAT_COLUMNS["maude"]}}]
    fields = {r["field"] for r in B.completeness_rows("maude", raw)}
    assert "event_type" not in fields and "udi_di" in fields


# ── run() with a fake client ─────────────────────────────────────────────────

def _completeness(source, n=100, k=90):
    cols = {f"c_{c}": k for c, _, _ in schemas.FLAT_COLUMNS[source]}
    return [{"slice_name": "overall", "slice_value": "all", "n_rows": n, **cols},
            {"slice_name": "year", "slice_value": "unknown", "n_rows": 40,
             **{c: 10 for c in cols}}]


CANNED = {
    "snapshot": [{"source": s, "n_rows": 100, "last_updated_at": "2026-10-26"} for s in schemas.SOURCES],
    "maude_link": [
        {"slice_name": "overall", "slice_value": "all", "n_rows": 1000, "n_udi": 500, "n_matched": 400, "n_bad_length": 5},
        {"slice_name": "manufacturer_tier", "slice_value": "long_tail", "n_rows": 200, "n_udi": 40, "n_matched": 10, "n_bad_length": 0},
    ],
    "unmatched_shape": [{"shape": "gs1_prefix_(01)", "n_unmatched": 60, "n_would_match_normalized": 50},
                        {"shape": "well_formed_not_in_gudid", "n_unmatched": 40, "n_would_match_normalized": 0}],
    "recall_enforcement_link": [
        {"link_name": "recall_to_enforcement", "slice_name": "overall", "slice_value": "all", "n_rows": 100, "n_linked": 80},
        {"link_name": "enforcement_to_recall", "slice_name": "overall", "slice_value": "all", "n_rows": 100, "n_linked": 90},
    ],
}


class FakeClient:
    """Dry runs return `gb`; real queries return canned rows picked by the SQL text."""
    def __init__(self, gb=0.1, empty=None):
        self.gb, self.empty, self.real = gb, empty, []

    def _name(self, sql):
        if "n_bad_length" in sql:
            return "maude_link"
        if "n_would_match_normalized" in sql:
            return "unmatched_shape"
        if "n_linked" in sql:
            return "recall_enforcement_link"
        if "UNION ALL\nSELECT" in sql and "MAX(last_updated_at)" in sql:
            return "snapshot"
        return next(f"completeness_{s}" for s in schemas.SOURCES
                    if f"FROM `{schemas.prod_table(P, s)}` AS t" in sql)

    def query(self, sql, job_config):
        if job_config.dry_run:
            return SimpleNamespace(total_bytes_processed=int(self.gb * 1e9))
        name = self._name(sql)
        self.real.append((name, job_config.maximum_bytes_billed))
        rows = [] if name == self.empty else (
            CANNED.get(name) or _completeness(name.removeprefix("completeness_")))
        return SimpleNamespace(result=lambda: rows, total_bytes_processed=123)


@pytest.fixture
def bq(fake_gcp):
    fake_gcp.bigquery.QueryJobConfig = lambda **kw: SimpleNamespace(**{"dry_run": False, **kw})
    return fake_gcp


def test_run_writes_csvs_and_summary(bq, tmp_path):
    client = FakeClient()
    assert B.run(client, P, tmp_path, max_gb=2.0) == B.EXIT_OK
    assert len(client.real) == 4 + len(schemas.SOURCES)
    assert all(limit == int(2.0 * 1e9) for _, limit in client.real)

    for name in ("udi_availability", "primary_di_match", "link_coverage", "field_completeness"):
        with (tmp_path / f"{name}.csv").open() as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == B.CSV_FIELDS
            assert list(reader)
    with (tmp_path / "unmatched_udi_shape.csv").open() as f:
        assert next(csv.DictReader(f))["shape"] == "gs1_prefix_(01)"

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["overall_rates"]["udi_availability:maude:udi_di"] == 0.5
    assert summary["overall_rates"]["primary_di_match:maude:udi_di"] == 0.8
    assert summary["overall_rates"]["link_maude_to_gudid:maude:udi_di"] == 0.4
    assert summary["overall_rates"]["link_recall_to_enforcement:recall:link_key"] == 0.8
    flagged = summary["disparities"]["udi_availability"]["top"]
    assert [r["slice_value"] for r in flagged] == ["long_tail"]
    assert summary["disparities"]["completeness"]["count"] > 25     # capped per metric
    assert len(summary["disparities"]["completeness"]["top"]) == 25
    assert summary["unmatched_udi_shape"]["n_unmatched"] == 100
    assert summary["limitations"] == B.LIMITATIONS
    assert set(summary["bytes"]) == {n for n, _ in B.queries(P)}


def test_run_stops_above_max_gb_before_any_real_query(bq, tmp_path):
    client = FakeClient(gb=5.0)
    assert B.run(client, P, tmp_path, max_gb=2.0) == B.EXIT_OVER_BUDGET
    assert client.real == [] and not list(tmp_path.iterdir())


def test_estimate_only_runs_no_real_query(bq, tmp_path):
    client = FakeClient()
    assert B.run(client, P, tmp_path, max_gb=2.0, estimate_only=True) == B.EXIT_OK
    assert client.real == []


def test_empty_result_fails_loudly(bq, tmp_path):
    with pytest.raises(RuntimeError, match="maude_link"):
        B.run(FakeClient(empty="maude_link"), P, tmp_path, max_gb=2.0)


def test_print_only_makes_no_gcp_call():
    out = subprocess.run([sys.executable, "scripts/analysis/bias_slices.py", "--project", P,
                          "--print-only"], cwd=REPO, capture_output=True, text=True, check=True)
    assert "-- maude_link" in out.stdout and "-- completeness_gudid" in out.stdout
