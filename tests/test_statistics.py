"""Unit tests for scripts/validation/statistics.py. No GCP calls."""
import datetime as dt
import json

import pytest

from scripts.validation import statistics as st
from scripts.warehouse import schemas

DAY = dt.date(2026, 10, 26)


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_sql_reads_one_partition_and_no_select_star(source):
    sql = st.build_stats_sql("p", source, DAY)
    assert "WHERE _ingest_date = DATE '2026-10-26'" in sql
    assert "SELECT *" not in sql
    assert sql.count(f"FROM `p.vigil_md_staging.{source}_stg`") == 1


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_sql_has_stats_for_every_column(source):
    sql = st.build_stats_sql("p", source, DAY)
    for col, typ, mode in schemas.FLAT_COLUMNS[source]:
        if mode == "REPEATED":
            assert f"AS {col}__avg_items" in sql and f"AS {col}__empty_pct" in sql
        else:
            assert f"AS {col}__null_pct" in sql
            if typ == "STRING":
                assert f"AS {col}__len_p99" in sql
                has_top = f"AS {col}__top_values" in sql
                assert has_top == (col not in schemas.MERGE_KEYS[source])
            if typ in ("DATE", "INT64"):
                assert f"AS {col}__min" in sql and f"AS {col}__max" in sql


def test_repeated_column_gets_array_stats_only():
    exprs = dict(st._exprs_for("product_codes", "STRING", "REPEATED"))
    assert set(exprs) == {"avg_items", "empty_pct"}


def test_parse_stats_row_nests_by_column():
    row = {"_rows": 10,
           "source_record_id__null_pct": 0.0,
           "record_date__min": dt.date(2002, 12, 5),
           "reason_text__top_values": [{"value": "x", "count": 3}]}
    parsed = st.parse_stats_row("recall", row)
    assert parsed["rows"] == 10
    assert parsed["columns"]["record_date"]["min"] == "2002-12-05"
    assert parsed["columns"]["reason_text"]["top_values"] == [{"value": "x", "count": 3}]
    assert set(parsed["columns"]) == {c for c, _, _ in schemas.FLAT_COLUMNS["recall"]}


def test_write_stats_path_and_json(tmp_path):
    doc = st.stats_document("p", "recall", DAY, {"rows": 1, "columns": {}},
                            generated_at=dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc))
    path = st.write_stats(doc, tmp_path)
    assert path == tmp_path / "recall" / "2026-10-26.json"
    saved = json.loads(path.read_text())
    assert saved["table"] == "p.vigil_md_staging.recall_stg" and saved["rows"] == 1
