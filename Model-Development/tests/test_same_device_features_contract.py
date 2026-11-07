"""Pins the Step 0 contract of scripts/same_device_features.py.

These tests need no function bodies. They check the constants against the README and the SQL that
already exists, so the module cannot start out different from the rest of the repo. Delete nothing
here when the bodies land: the real behaviour tests go in test_same_device_features.py.
"""
import inspect
import re
from pathlib import Path

import pytest

import same_device_features as f

ROOT = Path(__file__).resolve().parents[1]
SQL_FILES = sorted((ROOT / "sql").rglob("*.sql"))
PLACEHOLDER_IN_SQL = re.compile(r"r'(\^\(UNK\|[^']*\$)'")


def test_regexes_compile_in_python():
    re.compile(f.STRIP_CHARS_RE)
    re.compile(f.PLACEHOLDER_RE)


def test_placeholder_re_matches_the_readme():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f.PLACEHOLDER_RE in readme


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_placeholder_pattern_in_sql_is_the_shared_one(path):
    """A copy that differs by one alternative would label the same row differently in SQL and Python."""
    for found in PLACEHOLDER_IN_SQL.findall(path.read_text(encoding="utf-8")):
        assert found == f.PLACEHOLDER_RE, f"{path.name} has its own placeholder pattern"


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_strip_pattern_in_sql_is_the_shared_one(path):
    text = path.read_text(encoding="utf-8")
    for found in re.findall(r"REGEXP_REPLACE\(TRIM\([^)]*\),\s*r'([^']*)'", text):
        assert found == f.STRIP_CHARS_RE, f"{path.name} strips different characters"


def test_match_kind_is_the_q08_case_in_order():
    sql = (ROOT / "sql/same_device/08_udi_link_agreement.sql").read_text(encoding="utf-8")
    case = sql.split("-- @part crosstab")[1].split("AS model_status")[0]
    labels = re.findall(r"THEN '([a-z_]+)'", case) + re.findall(r"ELSE '([a-z_]+)'", case)
    assert [k.value for k in f.MatchKind] == labels


def test_feature_columns_are_clean():
    cols = f.FEATURE_COLUMNS
    assert len(cols) == len(set(cols))
    assert all(c.startswith("feat_") for c in cols)
    for c in cols:
        assert not f.is_banned_feature_name(c), c


@pytest.mark.parametrize("bad", ["udi_di", "feat_udi_prefix", "feat_gudid_di", "pair_id", "label",
                                 "feat_split", "device_key", "feat_di"])
def test_the_leak_check_flags_the_names_it_exists_for(bad):
    assert f.is_banned_feature_name(bad), bad


@pytest.mark.parametrize("ok", ["feat_distinctive", "feat_n_dis_for_model", "feat_gudid_model_missing",
                                "feat_identifier_exact", "feat_device_name_token_overlap"])
def test_the_leak_check_leaves_real_features_alone(ok):
    """A substring ban would reject these. That is the bug this test pins."""
    assert not f.is_banned_feature_name(ok), ok


def test_split_constants_are_sane():
    assert 0 < f.TEST_SPLIT_PCT < 100
    assert f.SUFFIX_LENS == (1, 2, 3)
    assert f.MIN_KEY_LEN == 2


EXPECTED_SIGNATURES = {
    "identifier_key": ["value"], "is_placeholder": ["key"], "usable_key": ["value"],
    "raw_equal": ["a", "b"], "contains_as_token": ["raw", "key"],
    "identifier_match": ["maude", "gudid"], "is_distinctive": ["n_dis_for_model"],
    "has_conflicting_identifier": ["maude", "gudid", "sibling_keys"],
    "pair_features": ["maude", "gudid", "ctx"], "feature_vector": ["features"],
    "assert_no_leakage": ["columns"], "assert_no_value_leakage": ["row", "di_value"],
    "device_key": ["gudid"], "split_bucket": ["key", "salt"],
    "split_of": ["key", "test_pct", "salt"],
    "pair_id": ["mdr_report_key", "device_seq", "gudid_udi_di", "role"],
    "sql_identifier_key": ["col"], "sql_is_placeholder": ["key_expr"], "sql_usable_key": ["col"],
    "sql_raw_equal": ["a", "b"], "sql_match_kind": ["m", "g"],
    "sql_device_key": ["mfr", "model", "catalog"],
    "sql_split": ["key_expr", "test_pct", "salt"], "sql_pair_id": ["mdr", "seq", "di", "role"],
}


@pytest.mark.parametrize("name,params", EXPECTED_SIGNATURES.items())
def test_signature_is_the_agreed_one(name, params):
    """Both of you code against these names. Changing one is a conversation, not an edit."""
    assert list(inspect.signature(getattr(f, name)).parameters) == params


def test_every_macro_points_at_an_emitter():
    for name, fn in f.SQL_MACROS.items():
        assert callable(fn) and fn.__name__ == f"sql_{name}"
