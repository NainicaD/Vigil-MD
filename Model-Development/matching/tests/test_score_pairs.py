"""s12: candidate pairs -> MATCH rows as links, the same in one process and in several. Offline."""
import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("xgboost")
pytest.importorskip("sklearn")

from xgboost import XGBClassifier  # noqa: E402

from scripts import s12_score_pairs as s12  # noqa: E402
from vigil_model import text_cosine  # noqa: E402
from vigil_model.same_device_features import DeviceSide  # noqa: E402

PACEMAKER = ("medtronic", "pacemaker", "A1", None, "DXY")
STENT = ("boston scientific", "WALLSTENT", "M001711260", None, "FGE")


@pytest.fixture
def bundle(tmp_path):
    """Tiny bundle, as in test_predict: the score rises with the text cosine."""
    cos = np.random.default_rng(0).uniform(0, 1, 400)
    model = XGBClassifier(n_estimators=20, max_depth=2)
    model.fit(pd.DataFrame({"feat_text_cosine": cos}), (cos > 0.5).astype(int))
    model.save_model(tmp_path / "model.json")
    gudid = [DeviceSide(*PACEMAKER[:4], (PACEMAKER[4],)),
             DeviceSide("medtronic", "pacemaker lead", "A2", None, ("DXY",)),
             DeviceSide(*STENT[:4], (STENT[4],))]
    text_cosine.save(text_cosine.fit(text_cosine.side_text(g) for g in gudid), tmp_path / "tfidf.pkl")
    (tmp_path / "meta.json").write_text(json.dumps({"cutoff": 0.5, "feature_columns": ["feat_text_cosine"]}))
    return tmp_path


def pair(maude_id, m, g):
    keys = ("mfr", "name", "model_raw", "catalog_raw")
    return {"maude_id": maude_id, "gudid_id": "g-" + maude_id, "meta_n_dis_for_model": "1",
            **{f"key_m_{k}": v for k, v in zip(keys, m)}, "key_m_pc": m[4],
            **{f"key_g_{k}": v for k, v in zip(keys, g)}, "key_g_pcs": g[4]}


@pytest.fixture
def files(tmp_path, bundle):
    pairs = pd.DataFrame([
        pair("same", PACEMAKER, PACEMAKER),                                   # MATCH
        pair("other", STENT, PACEMAKER),                                      # NON_MATCH
        pair("no_id", ("medtronic", "pacemaker", None, None, "DXY"), PACEMAKER),  # IE: no identifier
    ])
    pairs.to_parquet(tmp_path / "pairs.parquet", index=False)
    (tmp_path / "alias.csv").write_text("maude_labeler,gudid_labeler\n")
    return tmp_path


def run(files, *extra):
    out = files / "links.parquet"
    assert s12.main(["--pairs", str(files / "pairs.parquet"), "--bundle", str(files),
                     "--alias", str(files / "alias.csv"), "--siblings", str(files / "none.csv"),
                     "--out", str(out), *extra]) == 0
    return pd.read_parquet(out)


def test_only_match_rows_are_written_as_links(files):
    links = run(files)
    assert list(links.columns) == s12.LINK_COLUMNS
    assert links["left_id"].tolist() == ["same"] and links["right_id"].tolist() == ["g-same"]
    assert links["method"].tolist() == ["model"] and links["score"].iloc[0] > 0.5


def test_decisions_cover_match_non_match_and_ie(files):
    s12._init(files, files / "alias.csv", None)
    pairs = pd.read_parquet(files / "pairs.parquet")
    assert s12.score_chunk(pairs)["decision"].tolist() == ["MATCH", "NON_MATCH", "INSUFFICIENT_EVIDENCE"]


def test_parallel_batches_give_the_same_links(files):
    one = run(files).drop(columns="built_at")
    many = run(files, "--workers", "2", "--batch", "1").drop(columns="built_at")
    pd.testing.assert_frame_equal(one, many)


def test_report_has_decisions_scores_and_bundle(files):
    run(files, "--report", str(files / "report.json"))
    r = json.loads((files / "report.json").read_text())
    assert (r["n_pairs"], r["n_maude_rows"], r["n_match"], r["n_non_match"], r["n_insufficient_evidence"]) == (3, 3, 1, 1, 1)
    assert r["rate_match"] == pytest.approx(1 / 3)
    assert sum(r["score_hist"]) == 3 and len(r["score_hist"]) == 10
    assert r["score_p10"] <= r["score_p50"] <= r["score_p90"]
    assert r["cutoff"] == 0.5 and r["bundle_version"] == "unknown" and "score_psi" not in r


def test_psi_against_itself_is_zero_and_a_shift_warns_without_failing(files, capsys):
    run(files, "--report", str(files / "base.json"))
    run(files, "--report", str(files / "same.json"), "--baseline", str(files / "base.json"))
    assert json.loads((files / "same.json").read_text())["score_psi"] == pytest.approx(0)
    base = json.loads((files / "base.json").read_text())
    (files / "shifted.json").write_text(json.dumps({**base, "score_hist": [0] * 9 + [100]}))
    capsys.readouterr()
    run(files, "--report", str(files / "r.json"), "--baseline", str(files / "shifted.json"))
    assert json.loads((files / "r.json").read_text())["score_psi"] > s12.PSI_WARN
    assert "WARNING: score_psi" in capsys.readouterr().err


def chunk_lines(work):
    return [json.loads(line) for line in (work / "chunks.jsonl").read_text().splitlines()]


def test_work_dir_saves_one_part_and_one_stat_per_chunk(files):
    work = files / "work"
    links = run(files, "--batch", "1", "--work-dir", str(work), "--report", str(files / "r.json"))
    assert sorted(p.name for p in (work / "parts").glob("part-*.parquet")) == [
        "part-00000.parquet", "part-00001.parquet", "part-00002.parquet"]
    lines = chunk_lines(work)
    assert [s["chunk"] for s in lines] == [0, 1, 2] and all(s["n_chunks"] == 3 for s in lines)
    assert [(s["n_match"], s["n_non_match"], s["n_insufficient_evidence"]) for s in lines] == [
        (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    assert all(not s["resumed"] and s["max_rss_mb"] > 0 and s["pairs"] == 1 for s in lines)
    r = json.loads((files / "r.json").read_text())
    assert (r["n_chunks"], r["n_chunks_resumed"]) == (3, 0) and r["peak_rss_mb"] > 0 and r["pairs_per_s"] > 0
    assert links["left_id"].tolist() == ["same"]


def test_rerun_continues_from_the_saved_chunks(files):
    work = files / "work"
    first = run(files, "--batch", "1", "--work-dir", str(work)).drop(columns="built_at")
    (work / "parts" / "part-00001.parquet").unlink()               # chunk 1 was lost in a crash
    again = run(files, "--batch", "1", "--work-dir", str(work)).drop(columns="built_at")
    lines = sorted(chunk_lines(work), key=lambda s: s["chunk"])     # saved chunks are listed first
    assert [s["resumed"] for s in lines] == [True, False, True]
    pd.testing.assert_frame_equal(first, again)
    run(files, "--batch", "1", "--work-dir", str(work))
    assert all(s["resumed"] for s in chunk_lines(work))           # nothing left to score


def test_checkpoints_of_other_input_are_never_mixed(files):
    work = files / "work"
    run(files, "--batch", "1", "--work-dir", str(work))
    with pytest.raises(SystemExit, match="checkpoints of other input"):
        run(files, "--batch", "2", "--work-dir", str(work))         # other chunking
    pairs = pd.read_parquet(files / "pairs.parquet")
    pd.concat([pairs, pairs.head(1).assign(maude_id="extra")]).to_parquet(files / "pairs.parquet")
    with pytest.raises(SystemExit, match="checkpoints of other input"):
        run(files, "--batch", "1", "--work-dir", str(work))         # other candidates


def test_parallel_work_dir_gives_the_same_links_and_chunk_counts(files):
    one = run(files, "--batch", "1", "--work-dir", str(files / "w1")).drop(columns="built_at")
    two = run(files, "--batch", "1", "--workers", "2", "--work-dir", str(files / "w2")).drop(columns="built_at")
    pd.testing.assert_frame_equal(one, two)
    keep = ("chunk", "pairs", "n_match", "n_non_match", "n_insufficient_evidence", "resumed")
    strip = lambda lines: [{k: s[k] for k in keep} for s in lines]   # noqa: E731
    assert strip(chunk_lines(files / "w1")) == strip(chunk_lines(files / "w2"))


def test_streaming_chunks_cross_row_groups_and_give_the_same_result(files):
    import pyarrow as pa
    import pyarrow.parquet as pq
    base = pd.read_parquet(files / "pairs.parquet")
    seven = pd.concat([base.assign(maude_id=base["maude_id"] + f"-{k}") for k in range(3)]).head(7)
    pq.write_table(pa.Table.from_pandas(seven, preserve_index=False), files / "pairs.parquet",
                   row_group_size=2)                               # row groups of 2, chunks of 3
    one = run(files, "--batch", "7", "--report", str(files / "r1.json")).drop(columns="built_at")
    work = files / "w"
    many = run(files, "--batch", "3", "--work-dir", str(work), "--report", str(files / "r3.json")).drop(columns="built_at")
    pd.testing.assert_frame_equal(one, many)
    assert [s["pairs"] for s in chunk_lines(work)] == [3, 3, 1]
    r1, r3 = (json.loads((files / f).read_text()) for f in ("r1.json", "r3.json"))
    keep = ("n_pairs", "n_maude_rows", "n_match", "n_non_match", "n_insufficient_evidence",
            "score_hist", "match_kinds", "score_p10", "score_p50", "score_p90")
    assert {k: r1[k] for k in keep} == {k: r3[k] for k in keep} and r3["n_pairs"] == 7 and r3["n_maude_rows"] == 7


def test_no_match_chunks_still_write_a_typed_links_file(files):
    pd.read_parquet(files / "pairs.parquet").iloc[[1]].to_parquet(files / "pairs.parquet")   # NON_MATCH only
    links = run(files)
    assert len(links) == 0 and list(links.columns) == s12.LINK_COLUMNS


def test_gate_override_applies_m1_to_an_older_bundle(files):
    no_model = pair("nomodel", ("medtronic", "pacemaker", None, "A1", "DXY"), PACEMAKER)
    pd.DataFrame([no_model]).to_parquet(files / "pairs.parquet")   # catalog A1 = GUDID model: identifier agrees
    as_published = run(files, "--report", str(files / "a.json"))
    gated = run(files, "--ie-gate", "e6_e3b_c1_m1", "--report", str(files / "b.json"))
    assert json.loads((files / "b.json").read_text())["ie_gate"] == "e6_e3b_c1_m1"
    assert json.loads((files / "a.json").read_text())["ie_gate"] == "e6_e3b"          # the bundle names none
    # catalog equals the GUDID model: kind maude_catalog_eq_gudid_model, so M1 does not touch it
    assert len(as_published) == len(gated)


def test_gate_override_sends_no_model_no_agreement_pairs_to_ie(files):
    stent_family = pair("fam", ("medtronic", "pacemaker", None, "A9", "DXY"), PACEMAKER)   # catalog agrees with nothing
    pd.DataFrame([stent_family]).to_parquet(files / "pairs.parquet")
    run(files, "--report", str(files / "a.json"))
    run(files, "--ie-gate", "e6_e3b_c1_m1", "--report", str(files / "b.json"))
    a, b = (json.loads((files / f).read_text()) for f in ("a.json", "b.json"))
    assert b["n_insufficient_evidence"] == 1 and b["n_match"] == 0
    assert a["n_insufficient_evidence"] == 0                       # without the override: scored by the model


def test_a_changed_gate_never_reuses_checkpoints(files):
    work = files / "w"
    run(files, "--work-dir", str(work))
    with pytest.raises(SystemExit, match="checkpoints of other input"):
        run(files, "--work-dir", str(work), "--ie-gate", "e6_e3b_c1_m1")


def _links(*rows):
    return pd.DataFrame(rows, columns=["left_id", "right_id", "match_kind", "score"])


def test_one_link_per_reported_device_keeps_the_strongest_kind_over_a_higher_score():
    out = s12.best_link_per_device(_links(
        ("r1|1", "gA", "maude_catalog_eq_gudid_model", 0.999),    # catalog matches another record
        ("r1|1", "gB", "same_model_key", 0.96)))                  # model number matches: kept
    assert out["right_id"].tolist() == ["gB"]


def test_a_report_that_lists_two_devices_keeps_one_link_for_each():
    out = s12.best_link_per_device(_links(
        ("r1|1", "sensor", "same_model_key", 0.9),
        ("r1|2", "transmitter", "same_model_key", 0.9),
        ("r1|2", "other", "catalog_eq_catalog", 0.99)))
    assert sorted(out["right_id"]) == ["sensor", "transmitter"]


def test_score_then_right_id_break_ties_the_same_way_every_run():
    rows = [("r1|1", "gZ", "contains", 0.8), ("r1|1", "gB", "same_model_key", 0.9),
            ("r1|1", "gA", "same_model_key", 0.9)]
    for order in (rows, rows[::-1]):
        assert s12.best_link_per_device(_links(*order))["right_id"].tolist() == ["gA"]
    out = s12.best_link_per_device(_links(("r2|1", "g1", "contains", 0.7), ("r2|1", "g2", "conflict", 0.8)))
    assert out["right_id"].tolist() == ["g2"]                      # other kinds: highest score


def test_missing_input_column_fails_loudly(files):
    pd.read_parquet(files / "pairs.parquet").drop(columns="key_g_pcs").to_parquet(files / "pairs.parquet")
    with pytest.raises(SystemExit, match="key_g_pcs"):
        run(files)
