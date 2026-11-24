"""s14: precision, recall and candidate recall against the rows' own UDI. Offline, small fixed example."""
import json

import pandas as pd
import pytest

from scripts import s14_udi_agreement as s14


def cands():
    # 4 sampled rows (meta_n_pool_rows): m1 and m2 have their true group among the candidates,
    # m3 has only wrong candidates, m4 has no candidates at all.
    rows = [("m1", "g1", "A", "A"), ("m1", "g2", "B", "A"),
            ("m2", "g3", "C", "C"),
            ("m3", "g4", "D", "E")]
    return pd.DataFrame([{"maude_id": m, "gudid_id": g, "cand_group_key": c, "true_group_key": t,
                          "meta_n_pool_rows": 4} for m, g, c, t in rows])


def links(*pairs):
    """MATCH links as s12 writes them; (left_id, right_id[, match_kind, score])."""
    rows = [(*p, "same_model_key", 0.9)[:4] if len(p) == 2 else p for p in pairs]
    return pd.DataFrame(rows, columns=["left_id", "right_id", "match_kind", "score"])


def test_numbers_on_a_fixed_example():
    # m1 linked right, m2 missed, m3 linked wrong
    r = s14.agreement(cands(), links(("m1", "g1"), ("m3", "g4")))
    assert (r["n_rows"], r["n_rows_with_candidates"], r["n_pairs"], r["n_links"], r["n_links_right"]) == (4, 3, 4, 2, 1)
    assert r["candidate_recall"] == 0.5          # m1, m2 of 4
    assert r["precision"] == 0.5                 # g1 right, g4 wrong
    assert r["recall"] == 0.25                   # m1 of 4
    assert r["model_recall"] == 0.5              # m1 of m1, m2


def test_a_link_to_another_group_is_not_right():
    r = s14.agreement(cands(), links(("m1", "g2")))
    assert (r["precision"], r["recall"]) == (0.0, 0.0)


def test_no_links_gives_no_precision_and_zero_recall():
    r = s14.agreement(cands(), links())
    assert r["precision"] is None and r["recall"] == 0.0 and r["candidate_recall"] == 0.5


def test_an_extra_match_for_one_device_does_not_count_against_it():
    # m1's model number matches g1 (right); its catalog number matches g2 (another group)
    r = s14.agreement(cands(), links(("m1", "g1", "same_model_key", 0.95),
                                     ("m1", "g2", "maude_catalog_eq_gudid_model", 0.99)))
    assert (r["n_links_all"], r["n_links"], r["precision"]) == (2, 1, 1.0)


def test_pool_candidates_are_refused():
    with pytest.raises(SystemExit, match="udi_check"):
        s14.agreement(cands().assign(true_group_key=None), links())


def test_main_writes_the_report_with_the_bundle(tmp_path):
    cands().to_parquet(tmp_path / "c.parquet")
    links(("m1", "g1")).to_parquet(tmp_path / "l.parquet")
    (tmp_path / "meta.json").write_text(json.dumps({"version": "v1.1.0", "cutoff": 0.6}))
    assert s14.main(["--candidates", str(tmp_path / "c.parquet"), "--links", str(tmp_path / "l.parquet"),
                     "--bundle", str(tmp_path), "--out", str(tmp_path / "r.json")]) == 0
    r = json.loads((tmp_path / "r.json").read_text())
    assert r["bundle_version"] == "v1.1.0" and r["precision"] == 1.0
    assert r["ie_gate"] == "e6_e3b"                                   # the bundle names none
    s14.main(["--candidates", str(tmp_path / "c.parquet"), "--links", str(tmp_path / "l.parquet"),
              "--bundle", str(tmp_path), "--out", str(tmp_path / "r.json"), "--ie-gate", "e6_e3b_c1_m1"])
    assert json.loads((tmp_path / "r.json").read_text())["ie_gate"] == "e6_e3b_c1_m1"
