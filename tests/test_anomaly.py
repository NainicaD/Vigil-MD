"""Unit tests for scripts/warehouse/check_staging.py.

Pure functions only: no GCP calls, no network. Numbers come from the
enforcement baseline measured on 2026-10-26.
"""
import copy

import pytest

from scripts.warehouse import check_config
from scripts.warehouse import check_staging as cs

SRC = "enforcement"
RAW = "proj.vigil_md_raw.enforcement_raw"
STG = "proj.vigil_md_staging.enforcement_stg"


def make_cfg():
    return copy.deepcopy(check_config.CHECKS[SRC])


def make_profile(cfg, rows=40299, **overrides):
    p = {"rows_total": rows, "future_dates": 0, "before_1970": 0}
    for col in cfg["null_baseline_pct"]:
        p[f"miss__{col}"] = 0
        if col != cfg["date_column"]:
            p[f"ws__{col}"] = 0
    p.update(overrides)
    return p


def make_categories():
    return {
        "recall_class": {"Class II": 35598, "Class I": 3662, "Class III": 1039},
        "status": {"Terminated": 25334, "Ongoing": 14341, "Completed": 624},
    }


def one(results, check):
    found = [r for r in results if r.check == check]
    assert len(found) == 1, f"{check}: expected 1 result, got {len(found)}"
    return found[0]


# ── config ────────────────────────────────────────────────────────────────────

def test_enforcement_config_matches_schema():
    cs.validate_config(SRC, make_cfg())          # must not raise


def test_every_configured_source_matches_schema():
    for source, cfg in check_config.CHECKS.items():
        cs.validate_config(source, cfg)          # must not raise


def test_config_with_unknown_column_is_rejected():
    cfg = make_cfg()
    cfg["null_baseline_pct"]["not_a_column"] = 0.0
    with pytest.raises(ValueError):
        cs.validate_config(SRC, cfg)


def test_unknown_source_has_no_config():
    assert check_config.get_config("not_a_source") is None


def test_column_names_must_be_plain_identifiers():
    with pytest.raises(ValueError):
        cs.render_category_sql(STG, "status; DROP TABLE x")


# ── SQL ───────────────────────────────────────────────────────────────────────

def test_every_query_filters_the_partition():
    cfg = make_cfg()
    assert "_ingest_date = @ingest_date" in cs.render_counts_sql(RAW, STG)
    assert "_ingest_date = @ingest_date" in cs.render_profile_sql(STG, cfg)
    assert "_ingest_date = @ingest_date" in cs.render_category_sql(STG, "status")
    assert "_ingest_date >= DATE '1900-01-01'" in cs.render_previous_sql(STG)


def test_profile_sql_does_not_trim_the_date_column():
    sql = cs.render_profile_sql(STG, make_cfg())
    assert "TRIM(record_date)" not in sql
    assert "COUNTIF(record_date IS NULL) AS miss__record_date" in sql


def test_profile_sql_counts_every_configured_column():
    cfg = make_cfg()
    sql = cs.render_profile_sql(STG, cfg)
    for col in cfg["null_baseline_pct"]:
        assert f"miss__{col}" in sql


def test_profile_sql_product_code_variants():
    cfg = make_cfg()
    cfg["product_code"] = {"column": "product_code", "kind": "scalar"}
    assert "bad_product_codes" in cs.render_profile_sql(STG, cfg)
    cfg["product_code"] = {"column": "product_codes", "kind": "array"}
    assert "UNNEST(product_codes)" in cs.render_profile_sql(STG, cfg)


# ── raw to staged gap ─────────────────────────────────────────────────────────

def test_baseline_gap_passes():
    r = one(cs.evaluate_gap(make_cfg(), 40300, 40299), "raw_to_staged_gap")
    assert r.status == cs.PASS


def test_all_rows_dropped_always_fails():
    r = one(cs.evaluate_gap(make_cfg(), 40300, 0), "raw_to_staged_gap")
    assert r.status == cs.FAIL


def test_gap_warn_and_fail_limits():
    assert one(cs.evaluate_gap(make_cfg(), 10000, 9900), "raw_to_staged_gap").status == cs.WARN   # 1%
    assert one(cs.evaluate_gap(make_cfg(), 10000, 9700), "raw_to_staged_gap").status == cs.FAIL   # 3%


def test_more_staged_than_raw_warns():
    assert one(cs.evaluate_gap(make_cfg(), 100, 120), "raw_to_staged_gap").status == cs.WARN


# ── volume ────────────────────────────────────────────────────────────────────

def test_volume_without_previous_load_passes():
    assert one(cs.evaluate_volume(make_cfg(), 40299, None), "volume").status == cs.PASS


def test_volume_drop_limits():
    prev = {"rows_total": 40000, "ingest_date": "2026-10-19"}
    assert one(cs.evaluate_volume(make_cfg(), 39000, prev), "volume").status == cs.PASS   # -2.5%
    assert one(cs.evaluate_volume(make_cfg(), 30000, prev), "volume").status == cs.WARN   # -25%
    assert one(cs.evaluate_volume(make_cfg(), 15000, prev), "volume").status == cs.FAIL   # -62%


def test_volume_growth_is_not_flagged():
    prev = {"rows_total": 40000, "ingest_date": "2026-10-19"}
    assert one(cs.evaluate_volume(make_cfg(), 50000, prev), "volume").status == cs.PASS


# ── null rates ────────────────────────────────────────────────────────────────

def test_null_rate_at_baseline_passes():
    cfg = make_cfg()
    results = cs.evaluate_nulls(cfg, make_profile(cfg), small=False)
    assert {r.status for r in results} == {cs.PASS}


def test_null_rate_warn_and_fail():
    cfg = make_cfg()
    warn = make_profile(cfg, miss__event_id=2800)     # about 6.9%
    fail = make_profile(cfg, miss__event_id=6000)     # about 14.9%
    assert one(cs.evaluate_nulls(cfg, warn, False), "null_rate:event_id").status == cs.WARN
    assert one(cs.evaluate_nulls(cfg, fail, False), "null_rate:event_id").status == cs.FAIL


def test_null_record_date_is_checked_like_other_columns():
    cfg = make_cfg()
    fail = make_profile(cfg, miss__record_date=8000)
    assert one(cs.evaluate_nulls(cfg, fail, False), "null_rate:record_date").status == cs.FAIL


def test_small_batch_downgrades_rate_fail_to_warn():
    cfg = make_cfg()
    profile = make_profile(cfg, rows=100, miss__event_id=50)
    r = one(cs.evaluate_nulls(cfg, profile, small=True), "null_rate:event_id")
    assert r.status == cs.WARN
    assert "small batch" in r.note


# ── dates and product codes ───────────────────────────────────────────────────

def test_dates_clean_pass():
    cfg = make_cfg()
    assert one(cs.evaluate_dates(cfg, make_profile(cfg), False), "dates").status == cs.PASS


def test_a_few_bad_dates_warn_many_fail():
    cfg = make_cfg()
    assert one(cs.evaluate_dates(cfg, make_profile(cfg, future_dates=3), False), "dates").status == cs.WARN
    assert one(cs.evaluate_dates(cfg, make_profile(cfg, future_dates=1000), False), "dates").status == cs.FAIL


def test_product_codes_skipped_when_not_configured():
    cfg = make_cfg()
    assert cs.evaluate_product_codes(cfg, make_profile(cfg), False) == []


def test_product_codes_when_configured():
    cfg = make_cfg()
    cfg["product_code"] = {"column": "product_code", "kind": "scalar"}
    clean = make_profile(cfg, bad_product_codes=0)
    some = make_profile(cfg, bad_product_codes=5)
    many = make_profile(cfg, bad_product_codes=2000)
    assert one(cs.evaluate_product_codes(cfg, clean, False), "product_codes").status == cs.PASS
    assert one(cs.evaluate_product_codes(cfg, some, False), "product_codes").status == cs.WARN
    assert one(cs.evaluate_product_codes(cfg, many, False), "product_codes").status == cs.FAIL


# ── whitespace, text length, categories ───────────────────────────────────────

def test_product_code_limits_are_relative_to_the_baseline():
    cfg = make_cfg()
    cfg["product_code"] = {"column": "product_code", "kind": "scalar",
                           "baseline_bad_pct": 0.76, "warn_points": 0.25, "fail_points": 1.0}
    at_baseline = make_profile(cfg, rows=59552, bad_product_codes=454)    # 0.76%
    drifted = make_profile(cfg, rows=59552, bad_product_codes=900)        # 1.51%
    broken = make_profile(cfg, rows=59552, bad_product_codes=2000)        # 3.36%
    assert one(cs.evaluate_product_codes(cfg, at_baseline, False), "product_codes").status == cs.PASS
    assert one(cs.evaluate_product_codes(cfg, drifted, False), "product_codes").status == cs.WARN
    assert one(cs.evaluate_product_codes(cfg, broken, False), "product_codes").status == cs.FAIL


def test_empty_product_code_arrays_are_checked():
    cfg = make_cfg()
    cfg["product_code"] = {"column": "product_codes", "kind": "array", "baseline_empty_pct": 0.64}
    normal = make_profile(cfg, rows=100000, bad_product_codes=0, empty_product_codes=640)
    broken = make_profile(cfg, rows=100000, bad_product_codes=0, empty_product_codes=20000)
    assert one(cs.evaluate_product_codes(cfg, normal, False), "empty_product_codes").status == cs.PASS
    assert one(cs.evaluate_product_codes(cfg, broken, False), "empty_product_codes").status == cs.FAIL
    assert "empty_product_codes" in cs.render_profile_sql(STG, cfg)


def maude_profile(cfg, rows=1_000_000, **overrides):
    p = make_profile(cfg, rows=rows, bad_product_codes=0)
    for col in cfg["placeholder_baseline_pct"]:
        p[f"ph__{col}"] = 0
    p.update(overrides)
    return p


def test_counts_sql_adds_the_expected_rows_expression():
    sql = cs.render_counts_sql(RAW, STG, "GREATEST(1, 2)")
    assert "expected_staged_rows" in sql
    assert "SUM(GREATEST(1, 2))" in sql
    assert "_ingest_date = @ingest_date" in sql
    assert "expected_staged_rows" not in cs.render_counts_sql(RAW, STG)


def test_maude_gap_uses_the_expected_exploded_row_count():
    cfg = copy.deepcopy(check_config.CHECKS["maude"])
    counts = {"raw_rows": 21499653, "expected_staged_rows": 21532440, "staged_rows": 21532440}
    profile = maude_profile(cfg, rows=21532440)
    results = cs.evaluate_all(cfg, counts, profile, {}, None)
    assert one(results, "raw_to_staged_gap").status == cs.PASS
    assert not [r for r in results if r.check == "volume"]      # volume is off for MAUDE
    counts["staged_rows"] = 21000000                             # 2.5% short of what staging should make
    results = cs.evaluate_all(cfg, counts, profile, {}, None)
    assert one(results, "raw_to_staged_gap").status == cs.FAIL


def test_placeholder_check_against_baseline():
    cfg = copy.deepcopy(check_config.CHECKS["maude"])
    at_baseline = maude_profile(cfg, ph__lot_number=46960)       # 4.70%
    drifted = maude_profile(cfg, ph__lot_number=80000)           # 8.0%
    broken = maude_profile(cfg, ph__lot_number=150000)           # 15.0%
    assert one(cs.evaluate_placeholders(cfg, at_baseline, False), "placeholders:lot_number").status == cs.PASS
    assert one(cs.evaluate_placeholders(cfg, drifted, False), "placeholders:lot_number").status == cs.WARN
    assert one(cs.evaluate_placeholders(cfg, broken, False), "placeholders:lot_number").status == cs.FAIL


def test_placeholder_check_is_skipped_when_not_configured():
    cfg = make_cfg()
    assert cs.evaluate_placeholders(cfg, make_profile(cfg), False) == []
    assert "ph__" not in cs.render_profile_sql(STG, cfg)


def test_profile_sql_counts_placeholders_for_maude():
    cfg = copy.deepcopy(check_config.CHECKS["maude"])
    sql = cs.render_profile_sql(STG, cfg)
    assert "AS ph__lot_number" in sql
    assert "REGEXP_CONTAINS(TRIM(lot_number)" in sql


def test_whitespace_only_values_warn():
    cfg = make_cfg()
    assert one(cs.evaluate_whitespace(cfg, make_profile(cfg)), "whitespace_only").status == cs.PASS
    dirty = make_profile(cfg, ws__reason_text=4)
    assert one(cs.evaluate_whitespace(cfg, dirty), "whitespace_only").status == cs.WARN


def test_whitespace_tolerance_allows_a_handful_of_values():
    cfg = make_cfg()
    cfg["whitespace_max_pct"] = 0.001
    handful = make_profile(cfg, rows=21_532_440, ws__reason_text=3)
    many = make_profile(cfg, rows=21_532_440, ws__reason_text=50_000)     # about 0.23%
    ok = one(cs.evaluate_whitespace(cfg, handful), "whitespace_only")
    assert ok.status == cs.PASS and "reason_text=3" in ok.value
    assert one(cs.evaluate_whitespace(cfg, many), "whitespace_only").status == cs.WARN


def test_text_length_skipped_until_a_limit_is_set():
    cfg = make_cfg()
    assert cs.evaluate_text_lengths(cfg, make_profile(cfg)) == []
    cfg["text_max_length"] = {"reason_text": 1000}
    r = one(cs.evaluate_text_lengths(cfg, make_profile(cfg, maxlen__reason_text=5000)),
            "text_length:reason_text")
    assert r.status == cs.WARN


def test_known_categories_pass():
    results = cs.evaluate_categories(make_cfg(), make_categories())
    assert one(results, "allowed_values:recall_class").status == cs.PASS
    assert one(results, "allowed_values:status").status == cs.PASS
    assert one(results, "mix_shift:recall_class").status == cs.PASS


def test_new_category_value_warns():
    cats = make_categories()
    cats["recall_class"]["Class IX"] = 12
    r = one(cs.evaluate_categories(make_cfg(), cats), "allowed_values:recall_class")
    assert r.status == cs.WARN
    assert "Class IX" in r.value


def test_null_category_values_are_not_reported_as_new():
    cats = make_categories()
    cats["status"][None] = 3
    assert one(cs.evaluate_categories(make_cfg(), cats), "allowed_values:status").status == cs.PASS


def test_large_mix_shift_warns():
    cats = {"recall_class": {"Class II": 20000, "Class I": 15000, "Class III": 5299},
            "status": make_categories()["status"]}
    assert one(cs.evaluate_categories(make_cfg(), cats), "mix_shift:recall_class").status == cs.WARN


def test_mix_shift_ignored_for_small_batches():
    cats = {"recall_class": {"Class II": 5, "Class I": 5}, "status": {"Ongoing": 10}}
    results = cs.evaluate_categories(make_cfg(), cats)
    assert not [r for r in results if r.check.startswith("mix_shift")]


# ── whole run ─────────────────────────────────────────────────────────────────

def test_clean_baseline_run_has_no_warnings_or_failures():
    cfg = make_cfg()
    counts = {"raw_rows": 40300, "staged_rows": 40299}
    results = cs.evaluate_all(cfg, counts, make_profile(cfg), make_categories(), None)
    assert {r.status for r in results} == {cs.PASS}
    assert cs.exit_code(results) == 0


def test_run_where_nothing_reached_staging_fails():
    cfg = make_cfg()
    counts = {"raw_rows": 40300, "staged_rows": 0}
    results = cs.evaluate_all(cfg, counts, make_profile(cfg, rows=0), {}, None)
    assert one(results, "raw_to_staged_gap").status == cs.FAIL
    assert cs.exit_code(results) == cs.EXIT_DATA_FAIL


def test_warn_only_never_fails_the_run():
    results = [cs.Result("x", cs.FAIL, "1", "0")]
    assert cs.EXIT_DATA_FAIL == 3                      # shared with run_validation.py
    assert cs.exit_code(results, warn_only=False) == cs.EXIT_DATA_FAIL
    assert cs.exit_code(results, warn_only=True) == 0


def test_warnings_alone_do_not_fail_the_run():
    assert cs.exit_code([cs.Result("x", cs.WARN, "1", "0")]) == 0


def test_summary_and_format():
    results = [cs.Result("a", cs.PASS, "1", "2"), cs.Result("b", cs.FAIL, "3", "4", "why")]
    assert cs.summary(results) == "1 pass, 0 warn, 1 fail"
    assert "FAIL" in cs.format_results(results) and "[why]" in cs.format_results(results)