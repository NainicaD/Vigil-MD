"""Tests for pipeline/diag_training_issues.py. Offline: synthetic frames, MLflow only on a temp sqlite."""
import json
import math

import pandas as pd
import pytest

import pipeline.diag_training_issues as diag
from vigil_model.same_device_features import FEATURE_COLUMNS


def _feats(rows):
    """rows: (split, label, kind). Other features follow the kind, like the real table."""
    df = pd.DataFrame(rows, columns=["meta_split", "label", "feat_match_kind"])
    for col in FEATURE_COLUMNS:
        if col != "feat_match_kind":
            df[col] = False
    df["feat_n_dis_for_model"] = 1
    df["feat_raw_identifier_equal"] = df["feat_match_kind"] == "same_model_key"
    df["feat_identifier_exact"] = df["feat_raw_identifier_equal"]
    return df


LABEL_SET = _feats(
    [("train", "MATCH", "same_model_key")] * 4 + [("train", "NON_MATCH", "conflict")] * 4
    + [("train", "MATCH", "prefix_related"), ("train", "NON_MATCH", "prefix_related")]
    + [("test", "MATCH", "same_model_key")] * 2 + [("test", "NON_MATCH", "conflict")] * 2
    + [("test", "MATCH", "prefix_related")]
)


def test_kind_purity_finds_single_label_kinds():
    purity = diag.kind_purity(LABEL_SET)
    match, non_match = diag.pure_kinds(purity)
    assert match == {"same_model_key"} and non_match == {"conflict"}
    assert diag.share_in_pure_kinds(purity) == pytest.approx(8 / 10)
    row = purity[(purity.meta_split == "train") & (purity.feat_match_kind == "prefix_related")].iloc[0]
    assert (row.n_match, row.n_non_match, row.purity) == (1, 1, 0.5)


def test_rule_baseline_scores_the_test_split_only():
    r = diag.rule_baseline(LABEL_SET, {"same_model_key"})
    assert r["n"] == 5
    assert r["precision"] == 1.0 and r["recall"] == pytest.approx(2 / 3) and r["accuracy"] == pytest.approx(4 / 5)


def test_shortcut_trees_rebuild_the_label_without_kind_columns():
    big = pd.concat([LABEL_SET[LABEL_SET.feat_match_kind != "prefix_related"]] * 20, ignore_index=True)
    out = diag.shortcut_trees(big, depth=2)
    assert out["label_acc"] == 1.0                 # feat_raw_identifier_equal carries the kind
    assert out["feat_match_kind_acc"] == 1.0


def _cands():
    return pd.DataFrame({
        "pair_id": list("abcde"), "label": ["MATCH", "NON_MATCH", "INSUFFICIENT_EVIDENCE",
                                            "INSUFFICIENT_EVIDENCE", "NON_MATCH"],
        "cand_match_kind": ["same_model_key", "same_model_key", "maude_model_missing_or_placeholder",
                            "conflict", "conflict"],
        "hard_case_reason": ["none", "none", "E6_placeholder", "none", "none"],
        "w": [1.0, 1.0, 1.0, 1.0, 3.0], "score": [0.9, 0.9, 0.9, 0.1, 0.1]})


def test_accept_rates_keep_insufficient_evidence_rows():
    t = diag.accept_rates(_cands(), cutoff=0.5)
    ie = t[(t.by == "label") & (t.value == "INSUFFICIENT_EVIDENCE")].iloc[0]
    assert (ie.n, ie.accepted, ie.accept_rate) == (2, 1, 0.5)
    nm = t[(t.by == "label") & (t.value == "NON_MATCH")].iloc[0]
    assert nm.accept_rate == 0.5 and nm.accept_rate_weighted == pytest.approx(1 / 4)


def test_hand_prevalence_is_weighted_and_ignores_ie():
    assert diag.hand_prevalence(_cands()) == pytest.approx(1 / 5)


def test_prior_correction():
    assert diag.prior_corrected_precision(0.99, 0.9, 0.42, 0.42) == pytest.approx(0.99)
    assert diag.prior_corrected_precision(0.99, 0.9, 0.42, 0.10) < 0.99
    assert math.isnan(diag.prior_corrected_precision(None, 0.9, 0.42, 0.1))
    assert math.isnan(diag.prior_corrected_precision(0.0, 0.9, 0.42, 0.1))


def test_parity_reports_direction_of_a_mismatch():
    base = _feats([("x", "MATCH", "same_model_key")] * 2).assign(pair_id=["p1", "p2"])
    s05 = base.astype(str)                        # s05 writes a CSV, so its values arrive as text
    rebuilt = base.copy()
    rebuilt.loc[1, "feat_conflicting_identifier"] = True
    out = diag.parity(s05, rebuilt[["pair_id", *FEATURE_COLUMNS]]).set_index("feature")
    assert out.loc["feat_conflicting_identifier", "n_mismatch"] == 1
    assert out.loc["feat_conflicting_identifier", "rebuilt_true_s05_not"] == 1
    assert out["n_mismatch"].drop("feat_conflicting_identifier").sum() == 0


def test_rules_agreement():
    rules = pd.DataFrame({"pair_id": list("abcde"), "score": [0.95, 0.95, 0.2, 0.0, 0.0]})
    merged, s = diag.rules_agreement(_cands(), rules)
    assert (s["n_decided"], s["n_disagree"], s["n_ie"]) == (3, 1, 2)
    assert s["agree_rate"] == pytest.approx(2 / 3) and s["ie_rules_accept_rate"] == 0.0
    assert merged.loc[merged.pair_id == "b", "rules_agree"].iloc[0] == 0


def test_clean_metrics_drops_non_finite_and_fixes_names():
    out = diag.clean_metrics({"a b": 1, "x(y)": 2.0, "nan": float("nan"), "none": None, "s": "text"})
    assert out == {"a b": 1.0, "x_y_": 2.0}


def test_log_to_mlflow_writes_parent_and_child_runs(tmp_path, monkeypatch):
    mlflow = pytest.importorskip("mlflow")
    monkeypatch.chdir(tmp_path)                   # a new experiment's default artifact root is ./mlruns
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    parent_id = diag.log_to_mlflow(uri, {"diag_version": "d1"}, {"d1_share_train_rows_pure_kind": 0.96},
                                   {"xgb_all": {"d4_accept_rate_label_MATCH": 1.0, "d9_val_test_ap_gap": None}},
                                   {"xgb_all": {"diag_of_run_id": "abc"}})
    client = mlflow.tracking.MlflowClient(uri)
    parent = client.get_run(parent_id)
    assert parent.data.tags["track"] == "diag" and parent.data.metrics["d1_share_train_rows_pure_kind"] == 0.96
    exp = client.get_experiment_by_name(diag.EXPERIMENT)
    kids = client.search_runs([exp.experiment_id], f"tags.mlflow.parentRunId = '{parent_id}'")
    assert len(kids) == 1
    assert kids[0].data.tags["diag_of_run_id"] == "abc" and kids[0].data.tags["model_name"] == "xgb_all"
    assert kids[0].data.metrics == {"d4_accept_rate_label_MATCH": 1.0}
    assert client.list_artifacts(parent_id) == []      # artifacts only with --log-artifacts


def _sheet_rows():
    """Two MAUDE rows, one candidate each, in hand-sheet columns."""
    base = {"maude_manufacturer_norm": "acme", "maude_device_name": "pump", "maude_catalog_raw": "",
            "maude_product_code": "ABC", "cand_labeler": "acme", "cand_device_name": "pump", "cand_catalog": "",
            "cand_product_codes": "ABC", "cand_n_dis_for_model": "1", "meta_sample_weight": "2.0",
            "hard_case_reason": "", "cand_has_suffix_sibling": "FALSE"}
    rows = []
    for i, (model, cand, label, kind) in enumerate([("IP200", "IP200", "MATCH", "same_model_key"),
                                                    ("IP200", "ZZ9", "NON_MATCH", "conflict")]):
        mid = f"m{i}"
        rows.append({**base, "pair_id": f"{mid}-0", "maude_row_id": mid, "cand_rank": "0", "label": "MATCH" if i else "NON_MATCH",
                     "maude_model_raw": model, "cand_model": "", "cand_match_kind": ""})
        rows.append({**base, "pair_id": f"{mid}-1", "maude_row_id": mid, "cand_rank": "1", "label": label,
                     "maude_model_raw": model, "cand_model": cand, "cand_match_kind": kind})
    return pd.DataFrame(rows)


def test_main_end_to_end_without_mlflow(tmp_path):
    sheet = _sheet_rows()
    sheet.to_csv(tmp_path / "sheet.csv", index=False)
    cands = diag.hand_candidates(sheet)
    hand = diag.rebuild_hand_features(cands, frozenset(), {})
    hand.to_csv(tmp_path / "hand.csv", index=False)
    pd.concat([LABEL_SET] * 3, ignore_index=True).to_csv(tmp_path / "feats.csv", index=False)
    scores = tmp_path / "scores"
    scores.mkdir()
    for name in diag.MODELS:
        pd.DataFrame({"pair_id": hand["pair_id"], "score": [0.9, 0.9], "model_name": name,
                      "mlflow_run_id": "none"}).to_csv(scores / f"{name}_hand_scores.csv", index=False)
    (tmp_path / "cutoffs.json").write_text(json.dumps({m: 0.5 for m in diag.MODELS}))
    (tmp_path / "sib.csv").write_text("mfr,sib_key\n")
    (tmp_path / "alias.csv").write_text("maude_mfr,gudid_mfr\n")
    out = tmp_path / "out"
    args = ["--features", str(tmp_path / "feats.csv"), "--sheet", str(tmp_path / "sheet.csv"),
            "--hand-features", str(tmp_path / "hand.csv"), "--scores-dir", str(scores),
            "--cutoffs", str(tmp_path / "cutoffs.json"), "--siblings", str(tmp_path / "sib.csv"),
            "--alias", str(tmp_path / "alias.csv"), "--manifest", str(tmp_path / "none.json"),
            "--out", str(out), "--no-mlflow"]
    assert diag.main(args) == diag.EXIT_OK
    summary = json.loads((out / "summary.json").read_text())
    assert summary["d4"]["xgb_all"]["non_match_accepted"] == 1          # the conflict pair, scored 0.9
    assert summary["d6"]["mismatch"]["feat_match_kind"] == 0
    assert summary["d8"]["agree_rate"] == 1.0                           # s07 calls them the same way
    assert "Match-kind shortcut" in (out / "RESULTS.md").read_text()


def _learnable(n=40):
    """Train and test rows where feat_raw_identifier_equal decides the label, with device groups."""
    rows = [("train" if i % 5 else "test", "MATCH" if i % 2 else "NON_MATCH",
             "same_model_key" if i % 2 else "conflict") for i in range(n)]
    df = _feats(rows)
    df["meta_device_key"] = [f"d{i // 2}" for i in range(n)]
    df["pair_id"] = [f"p{i}" for i in range(n)]
    return df


XGB_LOGGED = {"n_estimators": "5", "max_depth": "2", "learning_rate": "0.3", "subsample": "1.0",
              "colsample_bytree": "1.0", "min_child_weight": "1"}


def test_explain_models_reproduces_and_attributes():
    feats = _learnable()
    hand = feats.head(10).astype(str)                     # the hand CSV arrives as text
    logged = {"xgb_all": {**XGB_LOGGED, "features": ",".join(FEATURE_COLUMNS)}, "logreg_v1": {}}
    first = diag.explain_models(feats, hand, logged, {"xgb_all": 0.5, "logreg_v1": 0.5}, {})
    assert not first["xgb_all"]["reproduced"]               # nothing saved to compare against
    saved = {n: pd.DataFrame({"pair_id": hand["pair_id"], "score": [0.0] * 10}) for n in logged}
    table = first["xgb_all"]["table"]
    assert set(table.index) == set(FEATURE_COLUMNS) and table["hand_mean_abs"].iloc[0] > 0
    assert set(first["logreg_v1"]["table"].index) >= {"feat_raw_identifier_equal", "kind_conflict"}
    # same params, same rows -> same scores: feed the first refit's scores back as the "saved" ones
    import xgboost  # noqa: F401  (the refit below needs it)
    X = diag.s06_encode(hand)
    train = feats[feats.meta_split == "train"].reset_index(drop=True)
    y = (train.label == "MATCH").astype(int).to_numpy()
    model = diag.refit_xgb(diag.s06_encode(train), y, diag.s06_fit_rows(diag.s06_encode(train), y, train.meta_device_key),
                           diag.xgb_params(XGB_LOGGED), list(FEATURE_COLUMNS))
    saved["xgb_all"]["score"] = model.predict_proba(X[list(FEATURE_COLUMNS)])[:, 1]
    again = diag.explain_models(feats, hand, {"xgb_all": logged["xgb_all"]}, {"xgb_all": 0.5}, saved)
    assert again["xgb_all"]["reproduced"] and again["xgb_all"]["repro_max_abs_diff"] <= diag.REPRO_TOL


def test_reproduction_gap_fails_on_different_or_missing_scores():
    ids = pd.Series(["a", "b"])
    saved = pd.DataFrame({"pair_id": ["a", "b"], "score": [0.1, 0.9]})
    assert diag.reproduction_gap(pd.Series([0.1, 0.9]).to_numpy(), ids, saved) == 0.0
    assert diag.reproduction_gap(pd.Series([0.1, 0.5]).to_numpy(), ids, saved) == pytest.approx(0.4)
    assert diag.reproduction_gap(pd.Series([0.1]).to_numpy(), pd.Series(["zz"]), saved) == float("inf")
    assert diag.reproduction_gap(pd.Series([0.1]).to_numpy(), pd.Series(["a"]), None) == float("inf")


def test_embedding_probe_separates_toy_texts():
    side = {"maude_manufacturer_norm": "acme", "maude_device_name": "infusion pump", "maude_catalog_raw": "",
            "maude_product_code": "FRN", "cand_labeler": "acme", "cand_catalog": "", "cand_product_codes": "FRN"}
    cands = pd.DataFrame([
        {**side, "pair_id": "a", "label": "MATCH", "cand_match_kind": "same_model_key", "maude_model_raw": "IP200",
         "cand_model": "IP200", "cand_device_name": "infusion pump"},
        {**side, "pair_id": "b", "label": "NON_MATCH", "cand_match_kind": "conflict", "maude_model_raw": "IP200",
         "cand_model": "ZX9", "cand_device_name": "bone screw"},
        {**side, "pair_id": "c", "label": "INSUFFICIENT_EVIDENCE", "cand_match_kind": "conflict",
         "maude_model_raw": "NA", "cand_model": "ZX9", "cand_device_name": "bone screw"}])
    index = ["acme | infusion pump | model ip200 | code frn", "acme | bone screw | model zx9 | code frn"] * 2
    probe, s = diag.embedding_probe(cands, index)
    assert s["auc_all"] == 1.0 and math.isnan(s["auc_ids_agree"])     # one class only where ids agree
    assert probe.set_index("pair_id").loc["a", "cosine"] > probe.set_index("pair_id").loc["b", "cosine"]
    assert "model" not in probe.set_index("pair_id").loc["c", "maude_text"]    # NA is a placeholder


def test_gudid_side_texts_dedupes_and_reads_code_arrays():
    import numpy as np
    ls = pd.DataFrame({"key_g_mfr": ["acme", "acme"], "key_g_name": ["pump", "pump"], "key_g_model_raw": ["IP200"] * 2,
                       "key_g_catalog_raw": [None, None], "key_g_pcs": [np.array(["FRN", "LZG"]), np.array(["FRN", "LZG"])]})
    assert diag.gudid_side_texts(ls) == ["acme | pump | model ip200 | code frn lzg"]


def test_main_refuses_missing_inputs(tmp_path):
    assert diag.main(["--features", str(tmp_path / "nope.parquet"), "--no-mlflow"]) == diag.EXIT_CONFIG
