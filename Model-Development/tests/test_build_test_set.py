"""Tests for the test-set sampler and labeling sheets. No real GCP: BigQuery is faked."""
import csv
import json
import re
from types import SimpleNamespace

import pytest

import bq_utils
import build_test_set as bts

PROJECT = "test-project"
SQL_FILES = sorted(bts.SQL_DIR.glob("*.sql"))
PLACEHOLDER_RE = (r"^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN"
                  r"|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$")


# ---------------------------------------------------------------------------- SQL files

def test_both_queries_exist():
    assert {p.stem for p in SQL_FILES} == {"01_sample", "02_candidates"}


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_every_query_has_a_full_header(path):
    header = bq_utils.load_query_file(path).header
    for key in bq_utils.HEADER_KEYS:
        assert header.get(key), f"{path.name}: missing '-- {key}:' header"


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_every_query_renders_read_only(path):
    sql = bts.rendered_sql(bq_utils.load_query_file(path), PROJECT)
    assert f"`{PROJECT}.vigil_md_prod." in sql
    bq_utils.assert_read_only(sql)


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_queries_use_the_shared_placeholder_rule(path):
    """The README's placeholder pattern must be copied exactly, on every identifier field."""
    text = path.read_text()
    found = re.findall(r"r'(\^\(UNK[^']*)'", text)
    assert found, f"{path.name}: no placeholder rule"
    assert set(found) == {PLACEHOLDER_RE}


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_queries_use_the_shared_model_key(path):
    text = path.read_text()
    assert "UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\\s\\-_./]', ''))" in text


def test_sample_query_declares_its_parameters():
    text = (bts.SQL_DIR / "01_sample.sql").read_text()
    assert "@per_stratum" in text and "@seed" in text


def test_candidate_query_uses_every_cap():
    text = (bts.SQL_DIR / "02_candidates.sql").read_text()
    for cap in bts.DEFAULT_CAPS:
        assert f"@{cap}" in text


# ---------------------------------------------------------------------------- sheets

def _sample(sid, stratum="cand_0|top_10", tuples=1000, rows=5000, n_rows=3):
    return {"sample_id": sid, "stratum": stratum, "stratum_tuples": tuples,
            "stratum_rows": rows, "n_rows": n_rows, "mfr": "acme", "model_key": "X100",
            "catalog_key": None, "name_key": "PUMP", "manufacturer_name_raw": "Acme Inc",
            "device_name_raw": "Pump", "model_number_raw": "X-100", "catalog_number": None,
            "product_code": "FRN", "example_mdr_report_key": "123", "example_device_seq": 0}


def _cand(sid, gk, rank=1, sources="id_exact"):
    return {"sample_id": sid, "group_key": gk, "best_rank": rank, "sources": sources,
            "n_dis": 1, "rep_udi_di": "000" + gk}


def test_weights_are_stratum_tuples_over_drawn():
    rows = bts.add_weights([_sample("a"), _sample("b"), _sample("c", "no_model|11_100", 90)])
    w = {r["sample_id"]: r["weight_tuple"] for r in rows}
    assert w == {"a": 500.0, "b": 500.0, "c": 90.0}
    assert {r["n_drawn"] for r in rows if r["stratum"] == "cand_0|top_10"} == {2}


def test_add_weights_does_not_change_its_input():
    rows = [_sample("a")]
    bts.add_weights(rows)
    assert "weight_tuple" not in rows[0]


def test_double_label_is_stable_and_near_the_share():
    ids = [f"id{i}" for i in range(2000)]
    first = [bts.double_label(i, "s", 0.2) for i in ids]
    assert first == [bts.double_label(i, "s", 0.2) for i in ids]
    assert 0.17 < sum(first) / len(ids) < 0.23
    assert not any(bts.double_label(i, "s", 0.0) for i in ids)
    assert all(bts.double_label(i, "s", 1.0) for i in ids)


def test_sample_sheet_counts_candidates_and_adds_label_columns():
    sheet = bts.build_sample_sheet([_sample("a"), _sample("b")],
                                   [_cand("a", "g1"), _cand("a", "g2")], "s", 0.2)
    n = {r["sample_id"]: r["n_candidates"] for r in sheet}
    assert n == {"a": 2, "b": 0}
    for col in bts.SAMPLE_LABEL_COLS:
        assert all(r[col] == "" for r in sheet)


def test_pair_sheet_keeps_tuples_without_candidates_and_sorts_by_rank():
    cands = [_cand("a", "g2", rank=3), _cand("a", "g1", rank=1)]
    sheet = bts.build_sample_sheet([_sample("a"), _sample("b")], cands, "s", 0.2)
    pairs = bts.build_pair_sheet(sheet, cands)
    assert [(p["sample_id"], p["group_key"]) for p in pairs] == [("a", "g1"), ("a", "g2"), ("b", "")]
    for col in bts.PAIR_LABEL_COLS:
        assert all(p[col] == "" for p in pairs)


def test_summarize_counts_strata_and_sources():
    cands = [_cand("a", "g1", sources="id_exact;id_fuzzy"), _cand("a", "g2", sources="random_same_mfr")]
    sheet = bts.build_sample_sheet([_sample("a"), _sample("b")], cands, "s", 0.0)
    summary = bts.summarize(sheet, cands)
    assert summary["tuples"] == 2 and summary["pairs"] == 2
    st = summary["strata"]["cand_0|top_10"]
    assert st["drawn"] == 2 and st["no_candidates"] == 1 and st["pool_tuples"] == 1000
    assert summary["pairs_by_source"] == {"id_exact": 1, "id_fuzzy": 1, "random_same_mfr": 1}


# ---------------------------------------------------------------------------- main

class FakeClient:
    """Answers dry runs with fixed sizes and real runs with fixed rows."""

    def __init__(self, sample_rows, cand_rows, gb=(1.0, 2.0)):
        self.sample_rows, self.cand_rows, self.gb = sample_rows, cand_rows, gb
        self.calls = []

    def query(self, sql, job_config=None):
        is_cands = "UNNEST(@sample)" in sql
        self.calls.append("02" if is_cands else "01")
        size = int((self.gb[1] if is_cands else self.gb[0]) * 1e9)
        rows = self.cand_rows if is_cands else self.sample_rows
        return SimpleNamespace(
            total_bytes_processed=size,
            result=lambda: [SimpleNamespace(items=lambda r=r: r.items()) for r in rows])


@pytest.fixture
def out_root(tmp_path, monkeypatch):
    monkeypatch.setattr(bts, "OUT_ROOT", tmp_path)
    return tmp_path


def test_print_only_needs_no_gcp(out_root, capsys):
    assert bts.main(["--project", PROJECT, "--print-only"]) == bts.EXIT_OK
    out = capsys.readouterr().out
    assert "-- 01_sample" in out and "-- 02_candidates" in out


def test_over_budget_runs_nothing(fake_bigquery, out_root):
    client = FakeClient([_sample("a")], [], gb=(1.0, 50.0))
    rc = bts.main(["--project", PROJECT, "--max-gb", "10"], client=client)
    assert rc == bts.EXIT_OVER_BUDGET
    assert client.calls == ["01", "02"]          # the two dry runs only
    assert not any(out_root.iterdir())


def test_estimate_only_runs_only_dry_runs(fake_bigquery, out_root):
    client = FakeClient([_sample("a")], [])
    assert bts.main(["--project", PROJECT, "--estimate-only"], client=client) == bts.EXIT_OK
    assert client.calls == ["01", "02"]


def test_full_run_writes_sheets_and_manifest(fake_bigquery, out_root):
    client = FakeClient([_sample("a"), _sample("b")], [_cand("a", "g1")])
    assert bts.main(["--project", PROJECT, "--seed", "s1"], client=client) == bts.EXIT_OK
    assert client.calls == ["01", "02", "01", "02"]
    d = out_root / "s1"
    with (d / "labeling_sheet.csv").open() as f:
        pairs = list(csv.DictReader(f))
    assert [(p["sample_id"], p["group_key"]) for p in pairs] == [("a", "g1"), ("b", "")]
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["seed"] == "s1" and manifest["tuples"] == 2 and manifest["pairs"] == 1
    assert manifest["bytes_processed"] == {"01_sample": int(1e9), "02_candidates": int(2e9)}


def test_existing_sheet_is_not_overwritten(fake_bigquery, out_root):
    (out_root / "s1").mkdir()
    (out_root / "s1" / "labeling_sheet.csv").write_text("labels in progress")
    client = FakeClient([_sample("a")], [])
    assert bts.main(["--project", PROJECT, "--seed", "s1"], client=client) == bts.EXIT_EXISTS
    assert client.calls == []
    assert (out_root / "s1" / "labeling_sheet.csv").read_text() == "labels in progress"
    assert bts.main(["--project", PROJECT, "--seed", "s1", "--overwrite"], client=client) == 0
