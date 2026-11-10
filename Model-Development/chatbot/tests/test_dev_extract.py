"""Tests for scripts/build_dev_extract.py: config checks and the SQL it builds. No GCP."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_dev_extract", ROOT / "scripts" / "build_dev_extract.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def cfg(**over):
    base = json.loads((ROOT / "data" / "dev_extract.json").read_text())
    return {**base, **over}


def write(tmp_path, data):
    p = tmp_path / "c.json"
    p.write_text(json.dumps(data))
    return p


def test_committed_config_is_valid():
    c = B.load_config()
    assert c["codes"] == ["LZG", "BZD", "OZD", "FTR", "PKU"]
    assert c["target_dataset"] == "vigil_md_dev" and c["source_dataset"] == "vigil_md_prod"


@pytest.mark.parametrize("over", [
    {"target_dataset": "vigil_md_prod"},                 # never write prod
    {"target_dataset": "vigil_md_staging"},
    {"product_codes": {"LZG'; DROP TABLE x; --": ""}},   # codes are inlined, so checked
    {"product_codes": {}},
    {"from_date": "2023-1-1"},
    {"version": "1"},
])
def test_bad_configs_are_refused(tmp_path, over):
    with pytest.raises(ValueError):
        B.load_config(write(tmp_path, cfg(**over)))


def test_every_table_reads_the_source_and_writes_the_target():
    c = B.load_config()
    for t in B.TABLE_ORDER:
        sql = B.create_sql(t, c)
        assert sql.startswith(f"CREATE OR REPLACE TABLE `{B.PROJECT}.vigil_md_dev.{t}`")
        assert f"FROM `{B.PROJECT}.vigil_md_prod`.{t}" in sql
        assert "('dev_extract_version', 'v1')" in sql


def test_layout_matches_prod():
    c = B.load_config()
    assert "PARTITION BY DATE_TRUNC(_part_date, MONTH)\nCLUSTER BY source_record_id, device_seq" in B.create_sql("maude", c)
    assert "CLUSTER BY mdr_report_key" in B.create_sql("maude_text", c)
    assert "PARTITION BY" not in B.create_sql("gudid", c)


def test_rows_follow_the_codes_and_the_copied_reports():
    c = B.load_config()
    assert "product_code IN ('LZG', 'BZD', 'OZD', 'FTR', 'PKU')" in B.select_sql("maude", c)
    assert "_part_date >= DATE '2023-01-01'" in B.select_sql("maude", c)
    for t in ("maude_problems", "maude_text"):
        assert f"SELECT DISTINCT source_record_id FROM `{B.PROJECT}.vigil_md_dev`.maude" in B.select_sql(t, c)
    assert f"FROM `{B.PROJECT}.vigil_md_dev`.recall" in B.select_sql("enforcement", c)
    assert "UNNEST(product_codes)" in B.select_sql("gudid", c)


def test_date_only_in_the_description_of_dated_tables():
    c = B.load_config()
    assert "from 2023-01-01" in B.create_sql("maude", c)
    assert "from 2023-01-01" not in B.create_sql("recall", c)


def test_no_mode_does_nothing(capsys):
    assert B.main([]) == 2
    assert "Nothing done" in capsys.readouterr().out
