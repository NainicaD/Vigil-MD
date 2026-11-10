"""s04: label_set_v1 -> feat_* table. No GCP: the SQL is checked as text, features in Python."""
import pytest

from scripts import s04_build_feature_table as s04
from vigil_model import rows
from vigil_model.same_device_features import FEATURE_COLUMNS


def row(**kw):
    """One label_set_v1 row, with the columns s04 reads."""
    base = {"pair_id": "p1", "label": "MATCH", "label_source": "udi_link", "meta_split": "train",
            "meta_device_key": "acme|X1", "meta_year": 2024, "key_match_kind": "same_model_key",
            "meta_n_dis_for_model": 3, "key_gudid_udi_di": "00812345678901",
            "key_m_mfr": "acme", "key_m_name": "PUMP", "key_m_model_raw": "GP200",
            "key_m_catalog_raw": None, "key_m_pc": "FRN",
            "key_g_mfr": "acme", "key_g_name": "Pump", "key_g_model_raw": "GP200",
            "key_g_catalog_raw": "GP200", "key_g_pcs": ["FRN"]}
    return {**base, **kw}


def build(records, siblings=None, ranks=None):
    pd = pytest.importorskip("pandas")
    return s04.build(pd.DataFrame(records), frozenset(), siblings or {}, ranks or {})


# --- the SQL -----------------------------------------------------------------------------------
def test_sibling_sql_renders_with_no_macros_left():
    sql = s04.sibling_sql("proj", "vigil_md_prod", "label_set_v1")
    assert "{{" not in sql and "}}" not in sql
    assert "proj.vigil_md_prod.gudid" in sql and "proj.vigil_md_model.label_set_v1" in sql


def test_sibling_sql_accepts_the_dev_dataset():
    assert "proj.vigil_md_dev.gudid" in s04.sibling_sql("proj", "vigil_md_dev", "label_set_v1")


def test_mfr_rank_sql_reads_only_maude_in_the_given_dataset():
    sql = s04.mfr_rank_sql("proj", "vigil_md_dev")
    assert "proj.vigil_md_dev.maude" in sql
    assert "gudid" not in sql and f"<= {s04.TIERS[1]}" in sql


def test_missing_input_fails_before_any_gcp_call(tmp_path, monkeypatch):
    """The query costs money; a missing local input must stop the run before it is spent."""
    def boom(*a, **k):
        raise AssertionError("made a GCP call despite a missing input")
    monkeypatch.setattr("google.cloud.bigquery.Client", boom, raising=False)
    code = s04.main(["--project", "p", "--data", str(tmp_path / "nope.parquet")])
    assert code == s04.EXIT_BAD_SQL


@pytest.mark.parametrize("sql", [
    "DELETE FROM `p.vigil_md_prod.gudid`",
    "SELECT 1 FROM `p.other_dataset.gudid`",
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1",
])
def test_guard_rejects_writes_and_foreign_datasets(sql):
    with pytest.raises(ValueError):
        s04.assert_select_datasets(sql, {"vigil_md_prod", "vigil_md_model"})


# --- the features ------------------------------------------------------------------------------
def test_columns_are_carry_then_features_in_order():
    out = build([row()])
    assert list(out.columns) == s04.META + list(FEATURE_COLUMNS)


# --- the manufacturer tier ------------------------------------------------------------------------
@pytest.mark.parametrize("rk,expected", [(1, "top_10"), (10, "top_10"), (11, "11_100"),
                                         (100, "11_100"), (None, "long_tail")])
def test_tier_follows_the_bias_report_boundaries(rk, expected):
    assert s04.tier_of("acme", {"acme": rk} if rk else {}) == expected


def test_tier_is_mfr_missing_when_maude_has_no_manufacturer():
    assert s04.tier_of(None, {"acme": 1}) == "mfr_missing"


def test_tier_comes_from_the_maude_side_not_the_candidate():
    """key_g_mfr is the GUDID labeler; the bias report ranks MAUDE report volume."""
    out = build([row(key_m_mfr="smallco", key_g_mfr="acme")], ranks={"acme": 1})
    assert out["meta_mfr_tier"][0] == "long_tail"


def test_tier_is_not_a_feature():
    out = build([row()], ranks={"acme": 1})
    assert "meta_mfr_tier" not in FEATURE_COLUMNS
    assert out["meta_mfr_tier"][0] == "top_10"


def test_no_identifier_or_di_column_survives():
    out = build([row()])
    leaked = [c for c in out.columns if c.startswith("key_") and c != "key_match_kind"]
    assert not leaked, leaked


def test_sibling_key_makes_a_suffix_candidate_a_conflict():
    # MAUDE GP200 exists in GUDID for this labeler, candidate is GP200S: E3a, NON_MATCH
    out = build([row(key_g_model_raw="GP200S", key_g_catalog_raw="GP200S")],
                siblings={"acme": frozenset({"GP200"})})
    assert bool(out["feat_conflicting_identifier"][0]) is True


def test_no_sibling_means_no_conflict():
    out = build([row(key_g_model_raw="GP200S", key_g_catalog_raw="GP200S")], siblings={})
    assert bool(out["feat_conflicting_identifier"][0]) is False


def test_exact_key_match_is_not_a_conflict():
    # pair de53171f: model 914ESP agrees; catalog 914ESPA is only a suffix away
    out = build([row(key_m_model_raw="914ESP", key_g_model_raw="914ESP", key_g_catalog_raw="914ESPA")],
                siblings={"acme": frozenset({"914ESP"})})
    assert bool(out["feat_conflicting_identifier"][0]) is False


def test_build_runs_the_di_value_leak_check(monkeypatch):
    """The guard skips bools, so today's 19 features can never trip it. This proves build()
    still calls it, so a future non-bool feature that carries the DI is caught."""
    from vigil_model.same_device_features import LeakageError
    leaky = dict.fromkeys(FEATURE_COLUMNS, False) | {"feat_match_kind": "00812345678901"}
    monkeypatch.setattr(s04, "pair_features", lambda *a, **k: leaky)
    with pytest.raises(LeakageError):
        build([row()])


# --- the cell helpers s04 and s05 share ----------------------------------------------------------
def test_product_codes_from_a_list_or_a_string():
    assert rows.product_codes(["FRN", "OZP"]) == ("FRN", "OZP")
    assert rows.product_codes("FRN;OZP") == ("FRN", "OZP")
    assert rows.product_codes(None) == ()


def test_product_codes_from_the_numpy_array_parquet_actually_gives():
    np = pytest.importorskip("numpy")
    assert rows.product_codes(np.array(["FRN", "OZP"])) == ("FRN", "OZP")
    assert rows.product_codes(np.array([])) == ()


def test_null_normalising_a_frame_keeps_array_cells_intact():
    """s04.main() does this before building; pd.notna on an array cell must not explode."""
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    df = pd.DataFrame({"key_g_pcs": [np.array(["FRN"]), None], "key_m_model_raw": ["GP200", float("nan")]})
    out = df.astype(object).where(pd.notna(df), None)
    assert list(out["key_g_pcs"][0]) == ["FRN"]
    assert out["key_g_pcs"][1] is None and out["key_m_model_raw"][1] is None


@pytest.mark.parametrize("value", [None, float("nan"), "", "  ", "<NA>", "nan"])
def test_text_treats_every_null_spelling_as_missing(value):
    assert rows.text(value) is None
