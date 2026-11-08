"""Tests for the hand-label sheet (N5). Offline: BigQuery is faked; nothing here runs SQL."""
import csv
import hashlib
import json
import re
from collections import Counter
from types import SimpleNamespace

import pytest

from vigil_model import bq_utils
from pipeline import s02_build_hand_label_sheet as hs
from vigil_model import same_device_features as f

PROJECT = "p"
SQL_FILES = sorted(hs.SQL_DIR.glob("*.sql"))
HARD_REASONS = ("E3b_prefix_only", "E4_non_distinctive", "E3_suffix_sibling", "E7_cross_field",
                "E5_alias_labeler", "E6_placeholder", "E9_kit_wording")   # most specific first


# ---------------------------------------------------------------------------- SQL

def test_the_three_queries_exist():
    assert [p.stem for p in SQL_FILES] == list(hs.QUERY_IDS)


@pytest.mark.parametrize("path", SQL_FILES, ids=lambda p: p.stem)
def test_header_and_no_rule_literals(path):
    text = path.read_text(encoding="utf-8")
    header = bq_utils.load_query_file(path).header
    for key in bq_utils.HEADER_KEYS:
        assert header.get(key), f"{path.name}: missing '-- {key}:'"
    assert not re.search(r"\br'", text), "regex literal: rules belong in same_device_features.py"
    assert not re.search(r"REGEXP_|UPPER\(", text), "normalization belongs in same_device_features.py"


def test_every_query_renders_and_reads_only_prod_and_model():
    sql = hs.load_sql(PROJECT)
    for qid, s in sql.items():
        assert "{{" not in s and not re.search(r"__[A-Z_]+__", s), qid
        bq_utils.assert_select_prod_model(s)
        assert f"`{PROJECT}.vigil_md_model.device_split`" in s, f"{qid} must use the stored split"


def _between(text, start, end):
    return text[text.index(start):text.index(end)]


def test_frame_is_identical_in_cells_and_draw():
    a = (hs.SQL_DIR / "01_cells.sql").read_text()
    b = (hs.SQL_DIR / "02_draw.sql").read_text()
    assert _between(a, "-- BEGIN frame", "-- END frame") == _between(b, "-- BEGIN frame", "-- END frame")


def test_frame_keeps_test_rows_only_and_splits_with_the_shared_macro():
    sql = hs.load_sql(PROJECT)["01_cells"]
    assert "WHERE f.frame_split = 'test'" in sql
    assert f.sql_split("p.m_base_key") in sql
    row = "CONCAT(p.mdr_report_key, '|', IFNULL(CAST(p.device_seq AS STRING), ''))"
    assert f.sql_split(row) in sql, "rows with no identifier are bucketed on mdr_report_key|device_seq"
    assert "ds.split" in sql, "a row whose key is a GUDID device takes that device's split"


def test_hard_case_reasons_are_all_present_in_priority_order():
    text = (hs.SQL_DIR / "01_cells.sql").read_text()
    case = _between(text, "-- The first hard-case reason", "END AS hard_case_reason")
    positions = [case.index(f"'{r}'") for r in HARD_REASONS]
    assert positions == sorted(positions)


def test_alias_is_hard_only_when_the_alias_makes_the_match():
    """E5 used to flag every row of a labeler with an alias (77% of the frame). It must need the key to
    exist under an alias labeler and not under the MAUDE name."""
    text = (hs.SQL_DIR / "01_cells.sql").read_text()
    assert "mg.n_dis IS NULL AND am.g_model IS NOT NULL" in text
    assert "JOIN model_groups AS mg ON mg.g_mfr = a.gudid_mfr" in text
    assert "alias_names" not in text


def test_hard_rows_are_stratified_by_reason_and_easy_rows_by_cell():
    text = (hs.SQL_DIR / "01_cells.sql").read_text()
    assert "IF(s.is_hard, CONCAT(s.hard_case_reason, '|hard'), CONCAT(s.cand_bucket, '|', s.mfr_size, '|easy'))" in text


def test_cells_are_counted_once_per_stratum():
    """A hard reason spans many cells. Grouping by cell as well returned it several times, and the
    script kept only the last count: the frame looked 884K rows smaller and hard weights were wrong."""
    text = (hs.SQL_DIR / "01_cells.sql").read_text()
    assert text.rstrip().endswith("FROM cells\nGROUP BY stratum_id\nORDER BY stratum_id")


def test_maude_row_id_matches_python():
    """The draw excludes earlier batches by maude_row_id, so SQL and Python must build it the same way."""
    text = (hs.SQL_DIR / "01_cells.sql").read_text()
    assert ("SUBSTR(TO_HEX(SHA256(CONCAT(p.mdr_report_key, '|', IFNULL(CAST(p.device_seq AS STRING), '')))), 1, 16)"
            in text)
    assert hs.maude_row_id("123", 0) == hashlib.sha256(b"123|0").hexdigest()[:16]
    assert hs.maude_row_id("123", None) == hashlib.sha256(b"123|").hexdigest()[:16]


def test_candidates_come_from_both_splits_and_carry_their_split():
    """Hiding train-split candidates turned true matches into "none of these"; the split is a column."""
    sql = hs.load_sql(PROJECT)["03_candidates"]
    assert "s.split = 'test'" not in sql
    assert f"LEFT JOIN `{PROJECT}.vigil_md_model.device_split` AS s USING (base_key)" in sql
    assert "IFNULL(gt.g_split, 'none') AS cand_split" in sql
    assert "cand_split" in hs.COLUMNS
    assert "x.cand_rank <= @max_cands" in sql
    assert f.sql_match_kind("m", "g") in sql
    assert f.sql_is_distinctive("gt.n_dis_for_model") in sql


def test_kit_word_macro_uses_the_shared_pattern():
    out = f.SQL_MACROS["has_kit_word"]("m_name")
    assert f.KIT_WORD_RE in out and "m_name" in out


# ---------------------------------------------------------------------------- read guard

def test_select_guard_accepts_prod_and_model_reads():
    bq_utils.assert_select_prod_model(
        "SELECT 1 FROM `p.vigil_md_prod.maude` JOIN `p.vigil_md_model.device_split` USING (k)")


@pytest.mark.parametrize("sql", [
    "CREATE OR REPLACE TABLE `p.vigil_md_model.x` AS SELECT 1 FROM `p.vigil_md_prod.maude`",
    "SELECT 1 FROM `p.vigil_md_staging.maude_stg`",
    "SELECT 1",
    "SELECT 1 FROM `p.vigil_md_prod.maude`; DELETE FROM `p.vigil_md_model.x` WHERE TRUE",
])
def test_select_guard_rejects(sql):
    with pytest.raises(ValueError):
        bq_utils.assert_select_prod_model(sql)


def test_read_only_guard_is_still_prod_only():
    with pytest.raises(ValueError):
        bq_utils.assert_read_only("SELECT 1 FROM `p.vigil_md_model.device_split`")


# ---------------------------------------------------------------------------- allocation

SIZES = {"cand_0|top_10|easy": 5000, "cand_0|smaller|easy": 3000, "no_model|11_100|easy": 2000,
         "cand_1|top_10|easy": 10, "E9_kit_wording|hard": 90000, "E4_non_distinctive|hard": 100,
         "E6_placeholder|hard": 1}


def test_allocation_totals_hard_share_and_floor():
    q = hs.allocate(SIZES, 150, 0.35, 2)
    assert sum(q.values()) == 150
    assert sum(v for k, v in q.items() if k.endswith("|hard")) == round(150 * 0.35)
    assert all(v >= min(2, SIZES[k]) for k, v in q.items() if k.endswith("|easy"))
    assert q["E6_placeholder|hard"] == 1                      # never more than the stratum has
    assert all(v <= SIZES[k] for k, v in q.items())


def test_hard_reasons_get_equal_shares_whatever_their_size():
    """A huge reason (here 90,000 rows) must not take the whole hard quota from a small one."""
    q = hs.allocate(SIZES, 150, 0.35, 2)
    assert abs(q["E9_kit_wording|hard"] - q["E4_non_distinctive|hard"]) <= 1
    q7 = hs.allocate({f"{r}|hard": 10_000 for r in HARD_REASONS} | {"x|y|easy": 10_000}, 150, 0.35, 2)
    hard = [v for k, v in q7.items() if k.endswith("|hard")]
    assert sum(hard) == 52 and max(hard) - min(hard) <= 1


def test_allocation_is_proportional_and_deterministic():
    q = hs.allocate(SIZES, 150, 0.35, 2)
    assert q["cand_0|top_10|easy"] > q["cand_0|smaller|easy"] > q["no_model|11_100|easy"] > q["cand_1|top_10|easy"]
    assert q["cand_1|top_10|easy"] == 2                        # the floor
    assert q == hs.allocate(dict(reversed(list(SIZES.items()))), 150, 0.35, 2)


def test_allocation_when_a_group_is_too_small():
    q = hs.allocate({"a|x|hard": 3, "b|x|easy": 1000}, 100, 0.5, 2)
    assert q["a|x|hard"] == 3 and q["b|x|easy"] == 97          # hard runs short; easy fills the total


def test_floor_too_high_is_a_config_error():
    with pytest.raises(ValueError):
        hs.allocate({f"c{i}|x|easy": 100 for i in range(20)}, 10, 0.0, 2)


def test_weights_are_inverse_inclusion_probability():
    w = hs.weights({"a|x|easy": 1000, "b|x|hard": 10}, Counter({"a|x|easy": 50, "b|x|hard": 5}))
    assert w == {"a|x|easy": 20.0, "b|x|hard": 2.0}


# ---------------------------------------------------------------------------- sheet

def _drawn(rid, stratum="cand_0|top_10|easy", mdr="100", seq=0, **kw):
    return {"maude_row_id": rid, "mdr_report_key": mdr, "device_seq": seq, "stratum_id": stratum,
            "cand_bucket": "cand_0", "mfr_size": "top_10", "is_hard": False, "hard_case_reason": None,
            "udi_status": "no_udi", "model_usable": True, "split_source": "maude_key_hash", "n_exact": 0,
            "m_mfr": "acme", "m_mfr_raw": "ACME INC", "m_name": "PUMP", "m_model_raw": "X-1",
            "m_catalog_raw": None, "m_model": "X1", "m_cat": None, "m_pc": "FRN", "m_date": "2024-03-01",
            "m_udi_di_raw": None, **kw}


def _cand(rid, rank, di, rule=1):
    return {"maude_row_id": rid, "cand_rank": rank, "cand_rule": rule, "cand_udi_di": di,
            "cand_labeler": "acme", "cand_labeler_raw": "Acme Inc", "cand_device_name": "Pump",
            "cand_model": "X1", "cand_catalog": None, "cand_product_codes": "FRN", "cand_n_dis_for_model": 1,
            "cand_is_distinctive": True, "cand_match_kind": "same_model_key",
            "cand_has_suffix_sibling": False, "cand_raw_identifier_equal": False}


def _sheet():
    drawn = [_drawn("r1", mdr="100", seq=0), _drawn("r2", mdr="200", seq=None, stratum="cand_0|top_10|hard")]
    cands = [_cand("r1", 2, "0002", rule=3), _cand("r1", 1, "0001")]
    return hs.build_sheet(drawn, cands, {"cand_0|top_10|easy": 25.0, "cand_0|top_10|hard": 4.0}, 1, "v1")


def test_sheet_has_a_none_row_then_ranked_candidates():
    rows = _sheet()
    assert [(r["maude_row_id"], r["cand_rank"]) for r in rows] == [("r1", 0), ("r1", 1), ("r1", 2), ("r2", 0)]
    assert rows[0]["cand_rule"] == "none_of_these" and rows[0]["cand_udi_di"] == ""
    assert rows[2]["cand_rule"] == "id_contains_same_labeler"


def test_sheet_columns_and_values():
    rows = _sheet()
    assert all(list(r) == list(hs.COLUMNS) for r in rows)
    assert all(r[c] == "" for r in rows for c in hs.LABELER_COLUMNS)
    assert rows[1]["accessgudid_url"].endswith("/0001") and rows[0]["accessgudid_url"] == ""
    assert rows[0]["openfda_url"].endswith("mdr_report_key:100")
    assert rows[1]["cand_is_distinctive"] == "TRUE" and rows[1]["cand_has_suffix_sibling"] == "FALSE"
    assert rows[0]["meta_sample_weight"] == 25.0 and rows[3]["meta_sample_weight"] == 4.0
    assert rows[0]["strat_year"] == "2024" and rows[3]["device_seq"] == ""


def test_pair_id_recomputes_with_the_shared_function():
    rows = _sheet()
    assert rows[0]["pair_id"] == f.pair_id("100", 0, hs.NONE_DI, hs.ROLE)
    assert rows[1]["pair_id"] == f.pair_id("100", 0, "0001", hs.ROLE)
    assert rows[3]["pair_id"] == f.pair_id("200", None, hs.NONE_DI, hs.ROLE)


def test_check_sheet_passes_and_catches_problems():
    rows = _sheet()
    hs.check_sheet(rows)
    with pytest.raises(ValueError):
        hs.check_sheet(rows + [dict(rows[1])])                      # duplicate pair_id
    with pytest.raises(ValueError):
        hs.check_sheet([r for r in rows if not (r["maude_row_id"] == "r1" and r["cand_rank"] == 0)])
    bad = [dict(r) for r in rows]
    bad[1]["label"] = "MATCH"
    with pytest.raises(ValueError):
        hs.check_sheet(bad)


def test_no_feature_column_in_the_sheet():
    assert not [c for c in hs.COLUMNS if c.startswith("feat_")]


# ---------------------------------------------------------------------------- main

class FakeClient:
    def __init__(self, cells, drawn, cands, gb=(3.0, 3.0, 1.0)):
        self.data = {"01_cells": cells, "02_draw": drawn, "03_candidates": cands}
        self.gb, self.calls = gb, []

    def query(self, sql, job_config=None):
        qid = ("03_candidates" if "UNNEST(@rows)" in sql else "02_draw" if "UNNEST(@quotas)" in sql
               else "01_cells")
        self.calls.append(qid)
        rows = self.data[qid]
        return SimpleNamespace(total_bytes_processed=int(self.gb[hs.QUERY_IDS.index(qid)] * 1e9),
                               result=lambda: [SimpleNamespace(items=lambda r=r: r.items()) for r in rows])


CELLS = [{"stratum_id": "cand_0|top_10|easy", "n_rows": 1000}, {"stratum_id": "cand_0|top_10|hard", "n_rows": 40}]
DRAWN = [_drawn("r1"), _drawn("r2", mdr="200", stratum="cand_0|top_10|hard")]
CANDS = [_cand("r1", 1, "0001")]


def test_print_only_needs_no_gcp(tmp_path, capsys):
    assert hs.main(["--project", PROJECT, "--print-only"], labels_dir=tmp_path) == hs.EXIT_OK
    assert "-- ===== 03_candidates =====" in capsys.readouterr().out


def test_estimate_only_runs_three_dry_runs(fake_bigquery, tmp_path):
    client = FakeClient(CELLS, DRAWN, CANDS)
    assert hs.main(["--project", PROJECT, "--estimate-only"], client=client, labels_dir=tmp_path) == hs.EXIT_OK
    assert client.calls == list(hs.QUERY_IDS)
    assert not any(tmp_path.iterdir())


def test_over_budget_runs_nothing(fake_bigquery, tmp_path):
    client = FakeClient(CELLS, DRAWN, CANDS, gb=(30.0, 3.0, 1.0))
    assert hs.main(["--project", PROJECT], client=client, labels_dir=tmp_path) == hs.EXIT_OVER_BUDGET
    assert client.calls == list(hs.QUERY_IDS)


def test_full_run_writes_the_sheet_and_manifest(fake_bigquery, tmp_path):
    client = FakeClient(CELLS, DRAWN, CANDS)
    assert hs.main(["--project", PROJECT, "--n", "4", "--floor", "1"], client=client,
                   labels_dir=tmp_path) == hs.EXIT_OK
    assert client.calls == list(hs.QUERY_IDS) * 2
    with (tmp_path / "hand_label_sample_v1.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["maude_row_id"], r["cand_rank"]) for r in rows] == [("r1", "0"), ("r1", "1"), ("r2", "0")]
    m = json.loads((tmp_path / "hand_label_sample_v1.manifest.json").read_text())
    assert m["maude_rows_drawn"] == 2 and m["rows_with_no_candidate"] == 1 and m["salt"] == "v1"
    assert m["cells"]["cand_0|top_10|easy"]["weight"] == 1000.0


def test_existing_sheet_is_not_overwritten(fake_bigquery, tmp_path):
    (tmp_path / "hand_label_sample_v1.csv").write_text("labels in progress")
    client = FakeClient(CELLS, DRAWN, CANDS)
    assert hs.main(["--project", PROJECT], client=client, labels_dir=tmp_path) == hs.EXIT_CONFIG
    assert client.calls == []
    assert (tmp_path / "hand_label_sample_v1.csv").read_text() == "labels in progress"


def test_batch_two_reads_the_exclusions(fake_bigquery, tmp_path):
    prev = tmp_path / "hand_label_sample_v1.csv"
    with prev.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["maude_row_id"])
        w.writeheader()
        w.writerows([{"maude_row_id": "r9"}, {"maude_row_id": "r9"}, {"maude_row_id": "r8"}])
    assert hs.read_exclusions([prev]) == ["r8", "r9"]
    client = FakeClient(CELLS, DRAWN, CANDS)
    rc = hs.main(["--project", PROJECT, "--n", "4", "--floor", "1", "--batch", "2", "--salt", "v2",
                  "--exclude", str(prev)],
                 client=client, labels_dir=tmp_path)
    assert rc == hs.EXIT_OK and (tmp_path / "hand_label_sample_v2.csv").exists()


def test_floor_that_cannot_fit_is_a_config_error_not_a_crash(fake_bigquery, tmp_path):
    three_easy = [{"stratum_id": f"cand_0|{s}|easy", "n_rows": 100} for s in ("top_10", "11_100", "smaller")]
    client = FakeClient(three_easy, DRAWN, CANDS)     # 3 easy rows to share, floor 2 x 3 cells = 6
    assert hs.main(["--project", PROJECT, "--n", "4", "--floor", "2"], client=client,
                   labels_dir=tmp_path) == hs.EXIT_CONFIG
    assert client.calls == list(hs.QUERY_IDS) + ["01_cells"]     # stopped before the draw
    assert not any(tmp_path.iterdir())


def test_a_stratum_returned_twice_stops_the_run(fake_bigquery, tmp_path):
    twice = CELLS + [{"stratum_id": "cand_0|top_10|hard", "n_rows": 5}]
    client = FakeClient(twice, DRAWN, CANDS)
    assert hs.main(["--project", PROJECT, "--n", "4", "--floor", "1"], client=client,
                   labels_dir=tmp_path) == hs.EXIT_CONFIG
    assert client.calls == list(hs.QUERY_IDS) + ["01_cells"]     # stopped before the draw
    assert not any(tmp_path.iterdir())


def test_bad_arguments_are_config_errors(tmp_path):
    assert hs.main(["--project", PROJECT, "--hard-share", "1.5"], labels_dir=tmp_path) == hs.EXIT_CONFIG
