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


# ── slices ────────────────────────────────────────────────────────────────────

def slice_cands(n_right=300, n_wrong=0, mfr="acme", model="AB12", event="Malfunction", groups=1):
    """n_right rows linked to their true group and n_wrong linked to another one, with candidate
    columns as s13 --mode udi_check writes them (groups = matched model groups per row)."""
    rows, lk = [], []
    for i in range(n_right + n_wrong):
        m, right = f"{mfr}-{i}", i < n_right
        rows.append({"maude_id": m, "gudid_id": f"{m}-g", "cand_group_key": "T" if right else "W",
                     "true_group_key": "T", "meta_n_pool_rows": 10_000, "key_m_mfr": mfr,
                     "key_m_pc": "FRN", "key_m_model_raw": model, "meta_event_type": event})
        lk.append((m, f"{m}-g", "same_model_key", 0.9))
        for g in range(1, groups):                         # extra matched groups, lower score
            rows.append({**rows[-1], "gudid_id": f"{m}-x{g}", "cand_group_key": f"X{g}"})
            lk.append((m, f"{m}-x{g}", "contains", 0.7))
    return pd.DataFrame(rows), pd.DataFrame(lk, columns=["left_id", "right_id", "match_kind", "score"])


def by(slices, field, value):
    return next(s for s in slices if s["slice"] == field and s["value"] == value)


def test_wilson_interval():
    assert s14.wilson(0, 0) is None
    lo, hi = s14.wilson(69, 100)
    assert 0.59 < lo < 0.60 and 0.77 < hi < 0.78          # the textbook 95% interval
    assert s14.wilson(100, 100)[1] == 1.0


def test_slices_measure_like_the_overall_check():
    c1, l1 = slice_cands(n_right=270, n_wrong=30, mfr="acme")
    c2, l2 = slice_cands(n_right=300, n_wrong=0, mfr="beta", model="UNK", event="Death")
    c, l = pd.concat([c1, c2]), pd.concat([l1, l2])
    sl = s14.slices(c, l, min_links=200)
    overall = s14.agreement(c, l)
    total = [s for s in sl if s["slice"] == "model_number"]
    assert sum(s["links"] for s in total) == overall["n_links"]
    assert sum(s["links_right"] for s in total) == overall["n_links_right"]
    assert by(sl, "manufacturer", "acme")["precision"] == 0.9
    assert by(sl, "model_number", "no model number")["links"] == 300      # "UNK" is a placeholder
    assert by(sl, "event_type", "Death")["precision"] == 1.0


def test_levels_need_enough_links_and_an_interval_under_the_level():
    c, l = slice_cands(n_right=240, n_wrong=60)            # 0.80, CI about 0.75-0.85
    s = by(s14.slices(c, l, min_links=200), "manufacturer_tier", "top 10 manufacturers")
    assert s["level"] == "alert" and s["precision_ci"][1] < 0.90
    c, l = slice_cands(n_right=93, n_wrong=7)              # 0.93 but only 100 links
    assert by(s14.slices(c, l, min_links=200), "manufacturer_tier", "top 10 manufacturers")["level"] == "small"
    c, l = slice_cands(n_right=1880, n_wrong=120)          # 0.94, CI 0.929-0.950: warn
    assert by(s14.slices(c, l, min_links=200), "candidates", "1")["level"] == "warn"
    c, l = slice_cands(n_right=2900, n_wrong=100)          # 0.967: ok
    assert by(s14.slices(c, l, min_links=200), "candidates", "1")["level"] == "ok"


def test_rows_matching_several_model_groups_are_their_own_slice():
    c1, l1 = slice_cands(n_right=250, mfr="one")
    c2, l2 = slice_cands(n_right=150, n_wrong=100, mfr="multi", groups=3)
    sl = s14.slices(pd.concat([c1, c2]), pd.concat([l1, l2]), min_links=200)
    assert by(sl, "matches", "0 or 1 model group")["links"] == 250
    multi = by(sl, "matches", "2 or more model groups")
    assert (multi["links"], multi["precision"]) == (250, 0.6)           # one link per device
    assert by(sl, "candidates", "2-3")["rows"] == 250


def test_small_manufacturers_are_left_out_and_event_type_is_optional():
    c, l = slice_cands(n_right=50, mfr="tiny")
    sl = s14.slices(c.drop(columns="meta_event_type"), l, min_links=200)
    assert not [s for s in sl if s["slice"] in ("manufacturer", "product_code", "event_type")]
    assert by(sl, "matches", "0 or 1 model group")["level"] == "small"


def test_flags_list_alerts_first_then_by_precision():
    sl = [{"slice": "a", "value": "x", "level": "warn", "precision": 0.93, "precision_ci": [0.92, 0.94], "links": 900},
          {"slice": "b", "value": "y", "level": "alert", "precision": 0.80, "precision_ci": [0.78, 0.82], "links": 500},
          {"slice": "c", "value": "z", "level": "ok", "precision": 0.99, "precision_ci": [0.98, 1.0], "links": 500}]
    assert s14.slice_flags(sl) == [
        "alert: b = y: precision 0.800 (95% CI 0.780-0.820, 500 links)",
        "warn: a = x: precision 0.930 (95% CI 0.920-0.940, 900 links)"]

