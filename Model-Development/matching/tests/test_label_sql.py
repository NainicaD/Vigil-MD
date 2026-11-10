"""Static checks on sql/labels/*.sql. Offline: nothing here talks to BigQuery."""
import re
from pathlib import Path

import pytest

from scripts import s01_build_label_set as build_labels
from vigil_model import same_device_features as f

LABEL_SQL = sorted((Path(__file__).resolve().parents[1] / "sql" / "labels").glob("*.sql"))
MACRO = re.compile(r"\{\{\s*(\w+)\((.*?)\)\s*\}\}")
KNOWN_PLACEHOLDERS = {"__MAUDE__", "__GUDID__", "__MODEL_DATASET__", "__MAX_PER_DEVICE__", "__NEG_PER_ANCHOR__",
                      "__CAND_POOL__", "__EASY_ANCHOR_PCT__", "__EASY_POOL__", "__AGREE_NEG_PER_ANCHOR__", "__PLACEHOLDER_NEG_PER_ANCHOR__",
                      "__LABEL_SET__"}


expand = build_labels.expand_macros


def test_there_are_label_sql_files():
    assert [p.name.split("_")[0] for p in LABEL_SQL] == ["00", "01", "02", "03", "04", "04b", "04c", "05", "06"]


def test_agree_negatives_keep_only_agreeing_kinds():
    """E1: the point of 04b is negatives whose identifiers agree; conflict/prefix/placeholder must not slip in."""
    text = (LABEL_SQL[0].parent / "04b_negatives_agree.sql").read_text(encoding="utf-8")
    kept = set(re.findall(r"'([a-z_]+)'", re.search(r"match_kind IN \((.*?)\)", text, re.S).group(1)))
    k = f.MatchKind
    assert kept == {k.SAME_MODEL_KEY, k.MAUDE_MODEL_EQ_GUDID_CATALOG, k.MAUDE_CATALOG_EQ_GUDID_MODEL,
                    k.CATALOG_EQ_CATALOG, k.CONTAINS}
    assert "udi_di != anchor_udi_di" in text and "c.meta_split = a.anchor_split" in text


@pytest.mark.parametrize("path", LABEL_SQL, ids=lambda p: p.name)
def test_macros_expand_and_leave_nothing_behind(path):
    out = expand(path.read_text(encoding="utf-8"))
    assert "{{" not in out and "}}" not in out
    assert set(re.findall(r"__[A-Z_]+__", out)) <= KNOWN_PLACEHOLDERS


@pytest.mark.parametrize("path", LABEL_SQL, ids=lambda p: p.name)
def test_no_regex_or_normalization_literals(path):
    """The one-file rule: rules come from macros. A raw regex or the pasted strip would drift from Python."""
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\br'", text), "regex literal"
    assert not re.search(r"REGEXP_|UPPER\(", text), "normalization belongs in the shared module"


@pytest.mark.parametrize("path", LABEL_SQL, ids=lambda p: p.name)
def test_match_kind_literals_are_real_kinds(path):
    kinds = {k.value for k in f.MatchKind}
    for lit in re.findall(r"match_kind\s*(?:!=|=)\s*'([a-z_]+)'", path.read_text(encoding="utf-8")):
        assert lit in kinds


@pytest.mark.parametrize("path", LABEL_SQL, ids=lambda p: p.name)
def test_every_part_is_one_write_to_the_model_dataset(path):
    out = path.read_text(encoding="utf-8")
    parts = re.split(r"^-- @part \w+$", out, flags=re.M)[1:] or [out]
    for part in parts:
        body = re.sub(r"--[^\n]*", "", part)
        assert len(re.findall(r"CREATE OR REPLACE TABLE", body)) == 1, path.name
        assert re.findall(r"CREATE OR REPLACE TABLE `__MODEL_DATASET__\.", body), "writes outside vigil_md_model"


def test_placeholder_negatives_need_a_usable_catalog_and_skip_prefix_relatives():
    """E3: a missing model with no usable catalog is IE (E6), and 71501 vs 71501-01 is IE (E3b): neither may
    become a NON_MATCH."""
    text = (LABEL_SQL[0].parent / "04c_negatives_placeholder.sql").read_text(encoding="utf-8")
    assert "NOT {{ is_placeholder(m_cat) }}" in text
    assert text.count("STARTS_WITH(a.m_cat, c.g_") == 2
    assert "WHERE match_kind = 'maude_model_missing_or_placeholder'" in text
    assert "c.meta_split = a.anchor_split" in text and "c.udi_di != a.anchor_udi_di" in text
