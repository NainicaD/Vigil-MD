"""Tests for N1 (bq_utils write guard and macro pass), N2 (00_device_split.sql) and N3
(01_alias_table.sql). Offline: nothing here talks to BigQuery, so these check the SQL text, the
guards and the build order, not query results."""
import re

import pytest

from vigil_model import bq_utils
from pipeline import s01_build_label_set as bl
from vigil_model import same_device_features as f

PROJECT = "p"
PARAMS = {"__MAX_PER_DEVICE__": 200, "__NEG_PER_ANCHOR__": 2, "__CAND_POOL__": 50,
          "__EASY_ANCHOR_PCT__": 10, "__EASY_POOL__": 20000}
SPLIT_SQL = bl.SQL_DIR / "00_device_split.sql"
ALIAS_SQL = bl.SQL_DIR / "01_alias_table.sql"


def _parts(path):
    qf = bq_utils.load_query_file(path)
    return {part: bl.render_part(sql, PROJECT, PARAMS) for part, sql in qf.parts}


# ---------------------------------------------------------------------------- N1: macro pass

def test_render_without_macros_is_unchanged():
    """macros=None must leave {{ }} alone, so the profiling queries and their tests do not change."""
    sql = "SELECT '{{ x() }}' FROM `__MAUDE__`"
    assert bq_utils.render(sql, PROJECT) == "SELECT '{{ x() }}' FROM `p.vigil_md_prod.maude`"


def test_macro_with_no_arguments():
    assert bq_utils.render("SELECT {{ suffix_lens() }}", PROJECT, macros=f.SQL_MACROS) == "SELECT [1, 2, 3]"


def test_macro_arguments_may_contain_parentheses_and_commas():
    out = bq_utils.expand_macros("{{ raw_equal(IFNULL(a, 'x'), b) }}", f.SQL_MACROS)
    assert out == f.sql_raw_equal("IFNULL(a, 'x')", "b")


def test_unknown_macro_wrong_arity_and_malformed_raise():
    for sql in ("{{ nope(a) }}", "{{ split(a, b, c, d) }}", "{{ split(a) }", "{{ split }}"):
        with pytest.raises(ValueError):
            bq_utils.expand_macros(sql, f.SQL_MACROS)


def test_macros_expand_before_placeholders():
    out = bq_utils.render("SELECT {{ identifier_key(m.x) }} FROM `__GUDID__`", PROJECT, macros=f.SQL_MACROS)
    assert out == f"SELECT {f.sql_identifier_key('m.x')} FROM `p.vigil_md_prod.gudid`"


# ---------------------------------------------------------------------------- N1: guards

def test_read_only_guard_still_blocks_create():
    """The write guard is separate; assert_read_only must not have been relaxed."""
    with pytest.raises(ValueError):
        bq_utils.assert_read_only("CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1")


def test_write_guard_accepts_one_model_write_reading_prod_and_model():
    bq_utils.assert_model_write_only(
        "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS "
        "SELECT * FROM `p.vigil_md_prod.gudid` JOIN `p.vigil_md_model.device_split` USING (k)")


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM `p.vigil_md_prod.maude`",                                            # not a write
    "CREATE TABLE `p.vigil_md_model.x` AS SELECT 1",                                    # not OR REPLACE
    "CREATE OR REPLACE TABLE `p.vigil_md_prod.x` AS SELECT 1",                          # writes prod
    "CREATE OR REPLACE TABLE `vigil_md_model.x` AS SELECT 1",                           # no project
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1 FROM `p.vigil_md_raw.y`",  # reads raw
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1; "
    "CREATE OR REPLACE TABLE `p.vigil_md_model.y` AS SELECT 2",                         # two writes
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1; DELETE FROM `p.vigil_md_model.y` WHERE TRUE",
])
def test_write_guard_rejects(sql):
    with pytest.raises(ValueError):
        bq_utils.assert_model_write_only(sql)


def test_build_labels_uses_the_shared_guard_and_macro_pass():
    assert bl.assert_model_write_only is bq_utils.assert_model_write_only
    assert bl.MODEL_DATASET == bq_utils.MODEL_DATASET == "vigil_md_model"
    assert not hasattr(bl, "MACRO") and not hasattr(bl, "WRITE_FORBIDDEN"), "local copies should be gone"


# ---------------------------------------------------------------------------- N2: device split

def test_split_file_builds_the_table_and_its_summary():
    parts = _parts(SPLIT_SQL)
    assert list(parts) == ["device_split", "device_split_summary"]
    assert bl.target_of(parts["device_split"]) == f"{PROJECT}.vigil_md_model.device_split"
    assert bl.target_of(parts["device_split_summary"]) == f"{PROJECT}.vigil_md_model.device_split_summary"


def test_split_outputs_the_columns_the_label_sql_reads():
    sql = _parts(SPLIT_SQL)["device_split"]
    final_select = sql[sql.rindex("\nSELECT"):]
    for col in ("base_key", "device_key", "split"):
        assert re.search(rf"\b{col},?\s*$|AS {col}\b", final_select, re.M), col


def test_base_key_is_the_shared_device_key():
    """02_positives.sql joins on base_key computed with the same macro; any other expression breaks the join."""
    sql = _parts(SPLIT_SQL)["device_split"]
    assert f.sql_device_key("manufacturer_name_norm", "model_number_raw", "catalog_number") in sql


def test_split_is_the_shared_hash_of_the_stemmed_key():
    """C6 recomputes split_of(device_key) in Python, so the SQL must hash device_key, not base_key."""
    sql = _parts(SPLIT_SQL)["device_split"]
    assert f"{f.sql_split('device_key')} AS split" in sql
    assert f.sql_split("base_key") not in sql


def test_stem_merge_uses_the_suffix_lengths_and_stops_at_two_levels():
    sql = _parts(SPLIT_SQL)["device_split"]
    assert f"UNNEST({f.sql_suffix_lens()}) AS d" in sql
    assert "COALESCE(p2.parent_key, p1.parent_key, k.base_key) AS device_key" in sql
    assert "p3.parent_key IS NOT NULL AS stem_truncated" in sql   # level 3 is only reported


def test_split_reads_gudid_only_and_has_no_filter_that_drops_keys():
    """Every GUDID base_key must be covered (05_label_set.sql errors on a missing one)."""
    sql = _parts(SPLIT_SQL)["device_split"]
    assert set(re.findall(r"`p\.vigil_md_\w+\.(\w+)`", sql)) == {"gudid", "device_split"}
    g_cte = sql[sql.index("WITH g AS"):sql.index("keys AS")]
    assert "WHERE" not in g_cte, "a filter on GUDID rows would leave base_keys without a split"


# ---------------------------------------------------------------------------- N3: alias table

def test_alias_file_builds_the_table_and_a_diagnostic():
    parts = _parts(ALIAS_SQL)
    assert list(parts) == ["alias_table", "alias_split_diagnostic"]
    assert bl.target_of(parts["alias_table"]) == f"{PROJECT}.vigil_md_model.alias_table"


def test_alias_table_uses_train_devices_only_and_the_shared_minimum():
    sql = _parts(ALIAS_SQL)["alias_table"]
    assert "s.split = 'train'" in sql and "'test'" not in sql
    assert f"HAVING COUNT(DISTINCT t.udi_di) >= {f.ALIAS_MIN_DIS}" in sql
    assert "ERROR('alias_table would be empty')" in sql
    for col in ("maude_mfr", "gudid_mfr"):
        assert f"AS {col}" in sql or f"t.{col}" in sql


def test_alias_min_dis_macro_follows_the_constant(monkeypatch):
    monkeypatch.setattr(f, "ALIAS_MIN_DIS", 7)
    assert f.SQL_MACROS["alias_min_dis"]() == "7"


def test_alias_uses_the_same_base_key_as_the_split():
    sql = _parts(ALIAS_SQL)["alias_table"]
    assert f.sql_device_key("manufacturer_name_norm", "model_number_raw", "catalog_number") in sql
    assert "USING (base_key)" in sql


# ---------------------------------------------------------------------------- build order

def test_00_and_01_are_not_behind_the_alias_gate():
    assert "00" < bl.ALIAS_GATED_FROM and "01" < bl.ALIAS_GATED_FROM


def test_build_order_and_inputs():
    files = bq_utils.load_query_dir(bl.SQL_DIR, ["00", "01"])
    items = [(qf.qid, part, sql) for qf, part, sql in bl.rendered(files, PROJECT, PARAMS)]
    order = [part for _, part, _ in items]
    assert order == ["device_split", "device_split_summary", "alias_table", "alias_split_diagnostic"]
    model = f"{PROJECT}.vigil_md_model."
    inputs = {part: bl.inputs_of(sql) for _, part, sql in items}
    assert inputs["device_split"] == set()
    assert inputs["alias_table"] == {model + "device_split"}
    assert inputs["alias_split_diagnostic"] == {model + "alias_table", model + "device_split"}
