"""Static checks on sql/labels/*.sql. Offline: nothing here talks to BigQuery."""
import re
from pathlib import Path

import pytest

from scripts import s01_build_label_set as build_labels
from vigil_model import same_device_features as f

LABEL_SQL = sorted((Path(__file__).resolve().parents[1] / "sql" / "labels").glob("*.sql"))
MACRO = re.compile(r"\{\{\s*(\w+)\((.*?)\)\s*\}\}")
KNOWN_PLACEHOLDERS = {"__MAUDE__", "__GUDID__", "__MODEL_DATASET__", "__MAX_PER_DEVICE__", "__NEG_PER_ANCHOR__",
                      "__CAND_POOL__", "__EASY_ANCHOR_PCT__", "__EASY_POOL__"}


expand = build_labels.expand_macros


def test_there_are_label_sql_files():
    assert [p.name[:2] for p in LABEL_SQL] == ["00", "01", "02", "03", "04", "05", "06"]


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
