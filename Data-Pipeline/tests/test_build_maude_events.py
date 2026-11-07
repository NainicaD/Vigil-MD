"""Tests for scripts/warehouse/build_maude_events.py (MAUDE problems and narrative text).

The SQL is checked as text. BigQuery is the fake_gcp MagicMock. No GCP.
"""
import re
import sys
import types
from datetime import date, timedelta

import pytest

from scripts.warehouse import build_maude_events as E
from scripts.warehouse import schemas

P = "p"
RAW = schemas.raw_table(P, "maude")
STG = schemas.maude_events_staging_table(P)
PROBLEMS = schemas.maude_event_prod_table(P, "maude_problems")
TEXT = schemas.maude_event_prod_table(P, "maude_text")
SENTINEL = "DATE '1900-01-01'"
TODAY = date.today().isoformat()


def staging_sql():
    return E.render_staging_sql(RAW, STG, E.read_events_sql())


def cols(columns):
    return [n for n, _, _ in columns]


def args(**kw):
    base = dict(text_types=None, event_types=None, from_year=None, to_year=None)
    return types.SimpleNamespace(**{**base, **kw})


# ── staging SQL ──────────────────────────────────────────────────────────────

def test_events_sql_file_is_valid():
    E.validate_events_sql(E.read_events_sql())


def test_staging_reads_one_raw_partition_and_replaces_one_staging_partition():
    sql = staging_sql()
    assert f"FROM `{RAW}` AS r" in sql and "r._ingest_date = @ingest_date" in sql
    assert f"DELETE FROM `{STG}` WHERE _ingest_date = @ingest_date;" in sql
    assert sql.index("BEGIN TRANSACTION") < sql.index("DELETE") < sql.index("INSERT") \
        < sql.index("COMMIT TRANSACTION")


def test_staging_insert_columns_match_schemas():
    insert = re.search(rf"INSERT INTO `{re.escape(STG)}` \(([^)]*)\)", staging_sql()).group(1)
    assert [c.strip() for c in insert.split(",")] == cols(schemas.MAUDE_EVENTS_STAGING_COLUMNS)


def test_staging_is_one_row_per_report():
    sql = staging_sql()
    assert "WHERE mdr_report_key IS NOT NULL" in sql
    assert "PARTITION BY mdr_report_key ORDER BY _loaded_at DESC, record_hash) = 1" in sql


def test_record_hash_covers_business_columns_only():
    struct = re.search(r"TO_JSON_STRING\(STRUCT\(([^)]*)\)\)", staging_sql()).group(1)
    assert [c.strip() for c in struct.split(",")] == E.BIZ_COLS
    assert not {"_ingest_date", "_loaded_at", "record_hash"} & set(E.BIZ_COLS)


def test_problem_arrays_are_distinct_and_skip_blank_terms():
    sql = E.read_events_sql()
    assert sql.count("SELECT DISTINCT TRIM(JSON_VALUE(") == 2
    assert "UNNEST(JSON_QUERY_ARRAY(pat, '$.patient_problems'))" in sql
    assert sql.count("IS NOT NULL") >= 2


@pytest.mark.parametrize("mutate, match", [
    (lambda s: s.replace("__RAW_TABLE__", "x"), "__RAW_TABLE__"),
    (lambda s: s.replace("r._ingest_date = @ingest_date", "TRUE"), "_ingest_date"),
    (lambda s: s.replace("AS patient_problems", "AS pp2"), "patient_problems"),
])
def test_validator_rejects_bad_sql(mutate, match):
    with pytest.raises(ValueError, match=match):
        E.validate_events_sql(mutate(E.read_events_sql()))


# ── prod SQL: replace each report's rows ─────────────────────────────────────

@pytest.mark.parametrize("name, render", [
    ("maude_problems", lambda: E.render_problems_sql(PROBLEMS, STG)),
    ("maude_text",     lambda: E.render_text_sql(TEXT, STG, dict(E.TEXT_FILTER))),
])
def test_prod_steps_replace_the_batch_reports_in_one_transaction(name, render):
    sql, prod = render(), schemas.maude_event_prod_table(P, name)
    order = ["BEGIN TRANSACTION", f"DELETE FROM `{prod}`", f"INSERT INTO `{prod}`",
             "SET dup_keys", "ROLLBACK TRANSACTION", "COMMIT TRANSACTION"]
    pos = [sql.index(s) for s in order]
    assert pos == sorted(pos)
    # delete only the batch's reports, pruned to the batch's months + the sentinel
    assert f"mdr_report_key IN (SELECT mdr_report_key FROM `{STG}` WHERE _ingest_date = @ingest_date)" in sql
    assert f"(_part_date BETWEEN @min_d AND @max_d OR _part_date = {SENTINEL})" in sql
    keys = ", ".join(schemas.MAUDE_EVENT_KEYS[name])
    assert f"COUNT(DISTINCT TO_JSON_STRING(STRUCT({keys})))" in sql


def test_problems_insert_both_problem_types():
    sql = E.render_problems_sql(PROBLEMS, STG)
    insert = re.search(rf"INSERT INTO `{re.escape(PROBLEMS)}` \(([^)]*)\)", sql).group(1)
    assert [c.strip() for c in insert.split(",")] == cols(schemas.MAUDE_PROBLEMS_COLUMNS)
    assert "'device', term" in sql and "UNNEST(s.product_problems)" in sql
    assert "'patient', term" in sql and "UNNEST(s.patient_problems)" in sql
    assert f"COALESCE(s.date_received, {SENTINEL})" in sql


def test_text_insert_columns_match_schemas():
    sql = E.render_text_sql(TEXT, STG, dict(E.TEXT_FILTER))
    insert = re.search(rf"INSERT INTO `{re.escape(TEXT)}` \(([^)]*)\)", sql).group(1)
    assert [c.strip() for c in insert.split(",")] == cols(schemas.MAUDE_TEXT_COLUMNS)


def test_text_filter_and_cleaning():
    sql = E.text_select_sql(STG, dict(E.TEXT_FILTER))
    assert "s.event_type IN UNNEST(@event_types)" in sql
    assert "text_type_code IN UNNEST(@text_types)" in sql
    assert "s.date_received BETWEEN @from_date AND @to_date" in sql
    assert "text IS NOT NULL" in sql                                      # empty text dropped
    assert "REGEXP_REPLACE(JSON_VALUE(t, '$.text')" in sql                # control characters
    assert "PARTITION BY mdr_report_key, mdr_text_key" in sql             # duplicate keys


def test_text_filter_all_drops_the_condition():
    sql = E.text_select_sql(STG, {**E.TEXT_FILTER, "event_types": None, "text_types": None})
    assert "@event_types" not in sql and "@text_types" not in sql
    assert "@from_date" in sql                                            # years always apply


def test_default_text_filter():
    assert E.TEXT_FILTER == {"text_types": ["Description of Event or Problem"],
                             "event_types": ["Death", "Injury"],
                             "from_year": 2016, "to_year": 2026}


def test_parse_filter_overrides():
    f = E.parse_filter(args(event_types=["all"], text_types=["Description of Event or Problem",
                                                               "Additional Manufacturer Narrative"],
                            from_year=2020))
    assert f["event_types"] is None and len(f["text_types"]) == 2 and f["from_year"] == 2020
    assert E.parse_filter(args()) == E.TEXT_FILTER
    with pytest.raises(ValueError):
        E.parse_filter(args(from_year=2026, to_year=2020))


def test_staging_expiry_guard():
    today = date(2026, 11, 20)
    assert E.staging_would_expire("2026-11-03", today)            # 17 days old
    assert not E.staging_would_expire("2026-11-10", today)        # 10 days old


# ── main (BigQuery faked) ────────────────────────────────────────────────────

def job(row=None, gb=1.0):
    j = types.SimpleNamespace(total_bytes_processed=int(gb * 1e9))
    j.result = lambda: iter([row])
    return j


@pytest.fixture
def bq(fake_gcp):
    """fake_gcp with a prod table size, as printed after a prod step."""
    table = fake_gcp.bigquery.Client.return_value.get_table.return_value
    table.table_id, table.num_bytes = "maude_x", 2_500_000_000
    return fake_gcp


def run_main(monkeypatch, *cli):
    monkeypatch.setattr(sys, "argv", ["build_maude_events.py", "--project", P, *cli])
    E.main()


def test_print_only_makes_no_gcp_call(monkeypatch, capsys):
    for mod in [m for m in sys.modules if m.startswith("google.cloud")]:
        monkeypatch.delitem(sys.modules, mod)
    for step in E.STEPS:
        run_main(monkeypatch, "--ingest-date", TODAY, "--step", step, "--print-only")
    assert "BEGIN TRANSACTION" in capsys.readouterr().out
    assert not any(m.startswith("google.cloud") for m in sys.modules)


def test_staging_refuses_an_expired_ingest_date(fake_gcp, monkeypatch):
    old = (date.today() - timedelta(days=20)).isoformat()
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, "--ingest-date", old, "--step", "staging")
    assert e.value.code == 1
    fake_gcp.bigquery.Client.return_value.query.assert_not_called()


def test_staging_stops_above_the_budget(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [job(types.SimpleNamespace(n=5)),      # raw count
                                job(gb=70.0)]                         # dry run
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, "--ingest-date", TODAY, "--step", "staging")
    assert e.value.code == E.EXIT_OVER_BUDGET
    assert client.query.call_count == 2                                # the script never ran


def test_staging_runs_with_maximum_bytes_billed(fake_gcp, monkeypatch, capsys):
    client = fake_gcp.bigquery.Client.return_value
    row = types.SimpleNamespace(staged_reports=4, with_product_problems=3, with_patient_problems=2)
    client.query.side_effect = [job(types.SimpleNamespace(n=5)), job(gb=48.8), job(row)]
    run_main(monkeypatch, "--ingest-date", TODAY, "--step", "staging")
    assert fake_gcp.bigquery.QueryJobConfig.call_args.kwargs["maximum_bytes_billed"] == int(60e9)
    assert "5 raw rows → 4 reports staged" in capsys.readouterr().out


def test_staging_fails_if_nothing_was_staged(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    row = types.SimpleNamespace(staged_reports=0, with_product_problems=0, with_patient_problems=0)
    client.query.side_effect = [job(types.SimpleNamespace(n=5)), job(), job(row)]
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, "--ingest-date", TODAY, "--step", "staging")
    assert e.value.code == 1


def test_empty_raw_partition_stages_nothing(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [job(types.SimpleNamespace(n=0))]
    run_main(monkeypatch, "--ingest-date", TODAY, "--step", "staging")
    assert client.query.call_count == 1


def test_prod_step_skips_an_empty_staging_partition(fake_gcp, monkeypatch):
    client = fake_gcp.bigquery.Client.return_value
    client.query.side_effect = [job(types.SimpleNamespace(n=0, min_d=None, max_d=None))]
    run_main(monkeypatch, "--ingest-date", "2026-11-03", "--step", "problems")
    assert client.query.call_count == 1


def test_problems_step_passes_the_batch_months(bq, monkeypatch, capsys):
    client = bq.bigquery.Client.return_value
    bounds = types.SimpleNamespace(n=3, min_d=date(2016, 1, 4), max_d=date(2026, 9, 30))
    client.query.side_effect = [job(bounds), job(gb=0.5),
                                job(types.SimpleNamespace(rows_deleted=2, rows_inserted=6, dup_keys=0))]
    run_main(monkeypatch, "--ingest-date", "2026-11-03", "--step", "problems")
    calls = bq.bigquery.ScalarQueryParameter.call_args_list
    assert ("min_d", "DATE", "2016-01-04") in [c.args for c in calls]
    assert ("max_d", "DATE", "2026-09-30") in [c.args for c in calls]
    assert "3 reports → 2 rows deleted, 6 inserted" in capsys.readouterr().out


def test_batch_without_dates_uses_the_sentinel(bq, monkeypatch):
    client = bq.bigquery.Client.return_value
    client.query.side_effect = [job(types.SimpleNamespace(n=1, min_d=None, max_d=None)), job(gb=0.1),
                                job(types.SimpleNamespace(rows_deleted=0, rows_inserted=1, dup_keys=0))]
    run_main(monkeypatch, "--ingest-date", "2026-11-03", "--step", "problems")
    calls = [c.args for c in bq.bigquery.ScalarQueryParameter.call_args_list]
    assert ("min_d", "DATE", "1900-01-01") in calls and ("max_d", "DATE", "1900-01-01") in calls


def test_text_step_sends_the_filter_as_parameters(bq, monkeypatch):
    client = bq.bigquery.Client.return_value
    bounds = types.SimpleNamespace(n=3, min_d=date(2026, 7, 1), max_d=date(2026, 9, 30))
    client.query.side_effect = [job(bounds), job(gb=0.5),
                                job(types.SimpleNamespace(rows_deleted=0, rows_inserted=4, dup_keys=0))]
    run_main(monkeypatch, "--ingest-date", "2026-11-03", "--step", "text", "--from-year", "2020")
    arrays = {c.args[0]: c.args[2] for c in bq.bigquery.ArrayQueryParameter.call_args_list}
    assert arrays == {"text_types": ["Description of Event or Problem"],
                      "event_types": ["Death", "Injury"]}
    scalars = [c.args for c in bq.bigquery.ScalarQueryParameter.call_args_list]
    assert ("from_date", "DATE", "2020-01-01") in scalars and ("to_date", "DATE", "2026-12-31") in scalars


def test_count_only_writes_nothing(fake_gcp, monkeypatch, capsys):
    client = fake_gcp.bigquery.Client.return_value
    bounds = types.SimpleNamespace(n=3, min_d=date(2026, 7, 1), max_d=date(2026, 9, 30))
    client.query.side_effect = [job(bounds), job(gb=2.0),
                                job(types.SimpleNamespace(text_rows=7, reports=3, text_mb=0.1))]
    run_main(monkeypatch, "--ingest-date", "2026-11-03", "--step", "text", "--count-only")
    script = client.query.call_args_list[-1].args[0]
    assert "INSERT" not in script and "DELETE" not in script
    assert "7 texts from 3 reports" in capsys.readouterr().out


def test_count_only_is_only_for_the_text_step(fake_gcp, monkeypatch):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "--ingest-date", "2026-11-03", "--step", "problems", "--count-only")


def test_default_caps_fit_a_full_export():
    # dry runs on the full 2026-11-03 export (2026-11-07): 48.8, 2.7, 32.3 GB
    for step, gb in {"staging": 48.8, "problems": 2.7, "text": 32.3}.items():
        assert gb < E.DEFAULT_MAX_GB[step] <= 2 * gb + 10, step
