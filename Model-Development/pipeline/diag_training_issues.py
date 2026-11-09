"""Diagnose the known training issues (D1-D9) and track the results in MLflow.

Offline: reads the local feature table, hand sheet, hand features, hand scores, cutoffs and the
training runs' metrics. No BigQuery. Writes reports/training_diag/ (one CSV per check,
summary.json, RESULTS.md).

MLflow: one parent run `training_diag` (tag track=diag) in vigil-md-linkage, with one nested run per
model whose tag diag_of_run_id names the training run it checks. Metrics and tags go to the
tracking store only. The experiment keeps artifacts on GCS, so the report files are uploaded only
with --log-artifacts.

Usage (from anywhere):
    python pipeline/diag_training_issues.py --no-mlflow     # report only, MLflow untouched
    python pipeline/diag_training_issues.py                 # report + diag runs in MLflow
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pipeline.s04_build_feature_table import read_siblings  # noqa: E402
from vigil_model.embedding_retrieval import TfidfEncoder, record_text  # noqa: E402
from vigil_model.rows import load_aliases, number, product_codes, text  # noqa: E402
from vigil_model.same_device_features import (  # noqa: E402
    FEATURE_COLUMNS, DeviceSide, MatchContext, MatchKind, pair_features,
)

MODELS = ("logreg_v1", "xgb_all", "xgb_no_match_kind", "xgb_no_kind_derived")
# same list as s06_train_models.py:44 (s06 trains at import time, so it cannot be imported)
KIND_DERIVED = ["feat_match_kind", "feat_identifier_exact", "feat_identifier_cross_field", "feat_prefix_related"]
NO_KIND_DERIVED = [c for c in FEATURE_COLUMNS if c not in KIND_DERIVED]
RULES_CUTOFF = 0.75            # s07_rules_baseline.RULES_CUTOFF
DECIDED = ("MATCH", "NON_MATCH")
IE = "INSUFFICIENT_EVIDENCE"
PLACEHOLDER_KIND = "maude_model_missing_or_placeholder"
ID_AGREE_KINDS = {"same_model_key", "maude_model_eq_gudid_catalog", "maude_catalog_eq_gudid_model",
                  "catalog_eq_catalog", "contains"}
XGB_PARAM_TYPES = {"n_estimators": int, "max_depth": int, "learning_rate": float, "subsample": float,
                   "colsample_bytree": float, "min_child_weight": int}
REPRO_TOL = 1e-4               # refit hand scores vs the saved ones (saved rounded to 6 places)
EXPERIMENT = "vigil-md-linkage"
DIAG_VERSION = "d2"
EXIT_OK, EXIT_CONFIG = 0, 2


# ---------------------------------------------------------------------------- encoding

def to_float(series: pd.Series) -> pd.Series:
    """Same as s06_train_models.to_float: True/False/number/empty -> 1.0/0.0/number/NaN."""
    mapped = series.astype(str).str.strip().str.upper().map({"TRUE": 1.0, "FALSE": 0.0})
    return mapped.fillna(pd.to_numeric(series, errors="coerce"))


def ratio(num, den) -> float:
    return float(num) / float(den) if den else float("nan")


# ---------------------------------------------------------------------------- D1-D3: label set

def kind_purity(feats: pd.DataFrame) -> pd.DataFrame:
    """D1. Rows per (split, match kind, label), and how pure each kind is."""
    t = (pd.crosstab([feats["meta_split"], feats["feat_match_kind"]], feats["label"])
         .reindex(columns=list(DECIDED), fill_value=0).rename_axis(columns=None).reset_index())
    t = t.rename(columns={"MATCH": "n_match", "NON_MATCH": "n_non_match"})
    t["n"] = t["n_match"] + t["n_non_match"]
    t["match_share"] = t["n_match"] / t["n"]
    t["purity"] = t[["n_match", "n_non_match"]].max(axis=1) / t["n"]
    return t


def pure_kinds(purity: pd.DataFrame, split: str = "train") -> tuple[set[str], set[str]]:
    """Kinds that are all MATCH and all NON_MATCH in `split`."""
    p = purity[purity["meta_split"] == split]
    return (set(p.loc[p["n_non_match"] == 0, "feat_match_kind"]),
            set(p.loc[p["n_match"] == 0, "feat_match_kind"]))


def share_in_pure_kinds(purity: pd.DataFrame, split: str = "train") -> float:
    p = purity[purity["meta_split"] == split]
    return ratio(p.loc[p["purity"] == 1.0, "n"].sum(), p["n"].sum())


def rule_baseline(feats: pd.DataFrame, match_kinds: set[str]) -> dict:
    """D2. 'MATCH iff the kind is all-MATCH in train', scored on the test split of the label set."""
    test = feats[feats["meta_split"] == "test"]
    pred = test["feat_match_kind"].isin(match_kinds).to_numpy()
    y = (test["label"] == "MATCH").to_numpy()
    tp, fp, fn = int((pred & y).sum()), int((pred & ~y).sum()), int((~pred & y).sum())
    return {"n": len(test), "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "accuracy": float((pred == y).mean()) if len(test) else float("nan")}


def encode(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: to_float(df[c]) for c in cols}, index=df.index).fillna(-1.0)


def shortcut_trees(feats: pd.DataFrame, cols: list[str] = NO_KIND_DERIVED, depth: int = 4,
                   sample: int = 200_000, seed: int = 0) -> dict:
    """D3. Shallow trees on the xgb_no_kind_derived features: can they rebuild the match kind, and
    the label? High accuracy means dropping the kind-derived columns removed no shortcut."""
    from sklearn.tree import DecisionTreeClassifier

    train, test = feats[feats["meta_split"] == "train"], feats[feats["meta_split"] == "test"]
    if len(train) > sample:
        train = train.sample(sample, random_state=seed)
    x_train, x_test = encode(train, cols), encode(test, cols)
    out = {"depth": depth, "train_rows": len(train), "test_rows": len(test)}
    for target in ("feat_match_kind", "label"):
        tree = DecisionTreeClassifier(max_depth=depth, random_state=seed).fit(x_train, train[target])
        out[f"{target}_acc"] = float((tree.predict(x_test) == test[target].to_numpy()).mean())
        out[f"{target}_majority_acc"] = float(test[target].value_counts(normalize=True).iloc[0])
    return out


# ---------------------------------------------------------------------------- D4-D5: hand sheet

def hand_candidates(sheet: pd.DataFrame) -> pd.DataFrame:
    """Candidate rows (cand_rank 1-3) with the columns the checks group by."""
    c = sheet[sheet["cand_rank"].astype(int) > 0].copy()
    c["w"] = c["meta_sample_weight"].astype(float)
    c["hard_case_reason"] = c["hard_case_reason"].fillna("").replace("", "none")
    c["cand_match_kind"] = c["cand_match_kind"].fillna("").replace("", "none")
    return c


def accept_rates(cands: pd.DataFrame, cutoff: float) -> pd.DataFrame:
    """D4. Share of candidate rows at or above the cutoff, by hand label (IE included), by match kind
    and by hard-case reason. s08 drops IE rows, so this is where they become visible."""
    accepted = cands["score"] >= cutoff
    rows = []
    for by in ("label", "cand_match_kind", "hard_case_reason"):
        for value, g in cands.groupby(by):
            a = accepted.loc[g.index]
            rows.append({"by": by, "value": value, "n": len(g), "accepted": int(a.sum()),
                         "accept_rate": float(a.mean()),
                         "accept_rate_weighted": ratio(g.loc[a, "w"].sum(), g["w"].sum())})
    return pd.DataFrame(rows)


def hand_prevalence(cands: pd.DataFrame) -> float:
    """Weighted MATCH share among candidate rows labeled MATCH or NON_MATCH: the pool's pair prior."""
    d = cands[cands["label"].isin(DECIDED)]
    return ratio(d.loc[d["label"] == "MATCH", "w"].sum(), d["w"].sum())


def prior_corrected_precision(precision, recall, prevalence, target) -> float:
    """D5. Precision the test split's TPR and FPR would give at another MATCH prevalence."""
    vals = (precision, recall, prevalence, target)
    if any(v is None or not math.isfinite(v) for v in vals) or precision <= 0 or recall <= 0 \
            or not 0 < prevalence < 1 or not 0 <= target <= 1:
        return float("nan")
    fpr = prevalence * recall * (1 - precision) / (precision * (1 - prevalence))
    return ratio(target * recall, target * recall + (1 - target) * fpr)


# ---------------------------------------------------------------------------- D6-D8

def hand_sides(r) -> tuple[DeviceSide, DeviceSide]:
    """The two sides of one hand-sheet row, built as s05_build_hand_features.py builds them."""
    maude = DeviceSide(manufacturer_norm=text(r["maude_manufacturer_norm"]), device_name=text(r["maude_device_name"]),
                       model_raw=text(r["maude_model_raw"]), catalog_raw=text(r["maude_catalog_raw"]),
                       product_codes=product_codes(r["maude_product_code"]))
    gudid = DeviceSide(manufacturer_norm=text(r["cand_labeler"]), device_name=text(r["cand_device_name"]),
                       model_raw=text(r["cand_model"]), catalog_raw=text(r["cand_catalog"]),
                       product_codes=product_codes(r["cand_product_codes"]))
    return maude, gudid


def rebuild_hand_features(cands: pd.DataFrame, aliases, siblings: dict[str, frozenset[str]]) -> pd.DataFrame:
    """D6. Hand features with sibling_keys looked up the s04 way (GUDID keys of the candidate's
    labeler) instead of s05's approximation from cand_has_suffix_sibling."""
    rows = []
    for _, r in cands.iterrows():
        maude, gudid = hand_sides(r)
        ctx = MatchContext(alias_pairs=aliases, n_dis_for_model=number(r["cand_n_dis_for_model"]),
                           sibling_keys=siblings.get(text(r["cand_labeler"]) or "", frozenset()))
        rows.append({"pair_id": r["pair_id"], **pair_features(maude, gudid, ctx)})
    return pd.DataFrame(rows, columns=["pair_id", *FEATURE_COLUMNS])


def parity(s05: pd.DataFrame, rebuilt: pd.DataFrame) -> pd.DataFrame:
    """D6. Per feature: rows where the s05 value differs from the s04-style rebuild."""
    m = s05[["pair_id", *FEATURE_COLUMNS]].merge(rebuilt, on="pair_id", suffixes=("_s05", "_rebuilt"))
    rows = []
    for col in FEATURE_COLUMNS:
        a, b = m[f"{col}_s05"], m[f"{col}_rebuilt"]
        if col == "feat_match_kind":
            diff = a.astype(str) != b.astype(str)
            s05_only = rebuilt_only = pd.Series(False, index=m.index)
        else:
            fa, fb = to_float(a), to_float(b)
            same = (fa.isna() & fb.isna()) | np.isclose(fa.fillna(-9e9), fb.fillna(-9e9))
            diff = ~same
            s05_only, rebuilt_only = diff & (fa == 1.0), diff & (fb == 1.0)
        rows.append({"feature": col, "n_compared": len(m), "n_mismatch": int(diff.sum()),
                     "s05_true_rebuilt_not": int(s05_only.sum()), "rebuilt_true_s05_not": int(rebuilt_only.sum()),
                     "example_pair_ids": ";".join(m.loc[diff, "pair_id"].head(5))})
    return pd.DataFrame(rows)


def unseen_kinds(hand_feats: pd.DataFrame, train_kinds: set[str]) -> pd.DataFrame:
    """D7. Hand rows whose match kind never occurs in the training split."""
    return hand_feats.loc[~hand_feats["feat_match_kind"].isin(train_kinds), ["pair_id", "feat_match_kind"]]


def rules_agreement(cands: pd.DataFrame, rules: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """D8. How often the hand label equals the guide's rule tree (s07). Near 1.0 means the hand set
    mostly re-states the rules, so it cannot show what the rules get wrong."""
    m = cands.merge(rules[["pair_id", "score"]].rename(columns={"score": "rules_score"}), on="pair_id", how="left")
    m["rules_accept"] = m["rules_score"] >= RULES_CUTOFF
    decided = m["label"].isin(DECIDED)
    m["rules_agree"] = np.where(decided, m["rules_accept"] == (m["label"] == "MATCH"), np.nan)
    ie = m["label"] == IE
    summary = {"n_decided": int(decided.sum()), "n_disagree": int((decided & (m["rules_agree"] == 0)).sum()),
               "agree_rate": float(m.loc[decided, "rules_agree"].mean()) if decided.any() else float("nan"),
               "n_ie": int(ie.sum()),
               "ie_rules_accept_rate": float(m.loc[ie, "rules_accept"].mean()) if ie.any() else float("nan")}
    return m, summary


def precision_on(rows: pd.DataFrame, cutoff: float) -> tuple[float, int]:
    """Precision and accepted count of a model on decided rows."""
    acc = rows[rows["score"] >= cutoff]
    return ratio((acc["label"] == "MATCH").sum(), len(acc)), len(acc)


# ---------------------------------------------------------------------------- D10: what the models learn
# s06 trains at import time, so its encode/lr_matrix/split are copied here. The reproduction gate
# (refit hand scores == saved hand scores) proves the copies still match s06.

def s06_encode(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of s06_train_models.encode: match kind as a fixed category, the rest as floats."""
    X = pd.DataFrame(index=df.index)
    for col in FEATURE_COLUMNS:
        if col == "feat_match_kind":
            X[col] = pd.Categorical(df[col], categories=[k.value for k in MatchKind])
        else:
            X[col] = to_float(df[col])
    return X


def s06_lr_matrix(X: pd.DataFrame) -> pd.DataFrame:
    """Copy of s06_train_models.lr_matrix."""
    M = pd.get_dummies(X["feat_match_kind"], prefix="kind", dtype=float)
    for col in FEATURE_COLUMNS:
        if col == "feat_match_kind":
            continue
        values = X[col]
        if values.isna().any():
            M[f"{col}_missing"] = values.isna().astype(float)
        M[col] = values.fillna(0.0)
    M["feat_n_dis_for_model"] = np.log1p(M["feat_n_dis_for_model"])
    labeler_ok = ((X["feat_labeler_equal"] == 1) | (X["feat_labeler_alias_equal"] == 1)).astype(float)
    M["cross_identifier_and_labeler"] = X["feat_identifier_exact"].fillna(0) * labeler_ok
    return M


def s06_fit_rows(X: pd.DataFrame, y: np.ndarray, groups: pd.Series) -> np.ndarray:
    """The rows s06 fits on: GroupShuffleSplit by device, test_size 0.2, seed 42."""
    from sklearn.model_selection import GroupShuffleSplit
    fit_idx, _ = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42).split(X, y, groups=groups))
    return fit_idx


def xgb_params(logged: dict) -> dict:
    """The logged (string) params of an s06 XGBoost run, typed back."""
    return {k: t(float(logged[k])) for k, t in XGB_PARAM_TYPES.items()}


def refit_xgb(X: pd.DataFrame, y: np.ndarray, fit_idx: np.ndarray, params: dict, cols: list[str]):
    from xgboost import XGBClassifier
    model = XGBClassifier(**params, tree_method="hist", enable_categorical=True, random_state=42, n_jobs=-1)
    return model.fit(X.iloc[fit_idx][cols], y[fit_idx])


def xgb_contributions(model, X: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """SHAP value per feature per row (XGBoost pred_contribs, as s10_explain_model does)."""
    import xgboost as xgb
    contrib = model.get_booster().predict(xgb.DMatrix(X[cols], enable_categorical=True), pred_contribs=True)
    return pd.DataFrame(contrib[:, :-1], columns=cols, index=X.index)


def shap_table(test_c: pd.DataFrame, hand_c: pd.DataFrame, wrong: np.ndarray) -> pd.DataFrame:
    """Mean |contribution| on the test sample and the hand rows, and the mean push on the hand
    false positives (positive = toward MATCH). Sorted by hand importance."""
    return pd.DataFrame({
        "test_mean_abs": test_c.abs().mean(),
        "hand_mean_abs": hand_c.abs().mean(),
        "false_pos_mean_push": hand_c[wrong].mean() if wrong.any() else np.nan,
    }).rename_axis("feature").sort_values("hand_mean_abs", ascending=False)


def reproduction_gap(new_scores: np.ndarray, pair_ids: pd.Series, saved: pd.DataFrame | None) -> float:
    """Largest |refit score - saved score| over the hand rows. inf when nothing to compare."""
    if saved is None:
        return float("inf")
    old = pair_ids.map(dict(zip(saved["pair_id"], saved["score"].astype(float))))
    if old.isna().any():
        return float("inf")
    return float(np.max(np.abs(new_scores - old.to_numpy()))) if len(old) else float("inf")


def explain_models(feats: pd.DataFrame, hand_feats: pd.DataFrame, params: dict[str, dict],
                   cutoffs: dict, saved: dict[str, pd.DataFrame], test_sample: int = 5000) -> dict:
    """D10. Refit every model from its logged params, check it reproduces the saved hand scores,
    and attribute its scores to features. LR contributions are coef * (x - fit mean)."""
    from sklearn.linear_model import LogisticRegression

    train = feats[feats["meta_split"] == "train"].reset_index(drop=True)
    y = (train["label"] == "MATCH").astype(int).to_numpy()
    X = s06_encode(train)
    fit_idx = s06_fit_rows(X, y, train["meta_device_key"])
    test = feats[feats["meta_split"] == "test"]
    test = test.sample(n=min(test_sample, len(test)), random_state=42)
    X_test, X_hand = s06_encode(test), s06_encode(hand_feats)
    out = {}
    for name, logged in params.items():
        if name == "logreg_v1":
            M = s06_lr_matrix(X)
            lr = LogisticRegression(max_iter=2000).fit(M.iloc[fit_idx], y[fit_idx])
            center, coef = M.iloc[fit_idx].mean(), lr.coef_[0]
            M_hand = s06_lr_matrix(X_hand).reindex(columns=M.columns, fill_value=0.0)
            M_test = s06_lr_matrix(X_test).reindex(columns=M.columns, fill_value=0.0)
            hand_score = lr.predict_proba(M_hand)[:, 1]
            hand_c, test_c = (M_hand - center) * coef, (M_test - center) * coef
        else:
            cols = logged["features"].split(",")
            model = refit_xgb(X, y, fit_idx, xgb_params(logged), cols)
            hand_score = model.predict_proba(X_hand[cols])[:, 1]
            hand_c, test_c = xgb_contributions(model, X_hand, cols), xgb_contributions(model, X_test, cols)
        cutoff = cutoffs.get(name)
        wrong = ((hand_score >= cutoff) & (hand_feats["label"] == "NON_MATCH").to_numpy()
                 if cutoff is not None else np.zeros(len(hand_feats), dtype=bool))
        gap = reproduction_gap(hand_score, hand_feats["pair_id"], saved.get(name))
        out[name] = {"table": shap_table(test_c, hand_c, wrong), "repro_max_abs_diff": gap,
                     "reproduced": gap <= REPRO_TOL, "n_false_pos": int(wrong.sum())}
    return out


# ---------------------------------------------------------------------------- D11: embedding probe

def gudid_side_texts(label_set: pd.DataFrame) -> list[str]:
    """The distinct GUDID records of the label set as embedding_retrieval text lines."""
    return sorted({record_text(r.key_g_mfr, r.key_g_name, r.key_g_model_raw, r.key_g_catalog_raw,
                               " ".join(product_codes(r.key_g_pcs)))
                   for r in label_set.itertuples()})


def pair_texts(cands: pd.DataFrame) -> tuple[list[str], list[str]]:
    maude = [record_text(r.maude_manufacturer_norm, r.maude_device_name, r.maude_model_raw,
                         r.maude_catalog_raw, r.maude_product_code) for r in cands.itertuples()]
    cand = [record_text(r.cand_labeler, r.cand_device_name, r.cand_model, r.cand_catalog,
                        r.cand_product_codes) for r in cands.itertuples()]
    return maude, cand


def label_auc(df: pd.DataFrame, score_col: str = "cosine") -> float:
    """AUC of a score for MATCH vs NON_MATCH (IE left out). NaN when one class is missing."""
    from sklearn.metrics import roc_auc_score
    d = df[df["label"].isin(DECIDED)]
    y = (d["label"] == "MATCH").to_numpy()
    return float(roc_auc_score(y, d[score_col])) if 0 < y.sum() < len(y) else float("nan")


def embedding_probe(cands: pd.DataFrame, index_texts: list[str]) -> tuple[pd.DataFrame, dict]:
    """D11. TF-IDF character n-grams (the embedding prototype's baseline), fitted on GUDID texts.
    Cosine of each hand pair: does text similarity separate MATCH from NON_MATCH, in particular
    where the identifiers already agree (where the models make their false positives)?"""
    enc = TfidfEncoder()
    enc.fit(index_texts)
    maude, cand = pair_texts(cands)
    cosine = np.asarray(enc.encode(maude).multiply(enc.encode(cand)).sum(axis=1)).ravel()
    probe = cands[["pair_id", "label", "cand_match_kind"]].assign(maude_text=maude, cand_text=cand, cosine=cosine)
    agree = probe["cand_match_kind"].isin(ID_AGREE_KINDS)
    summary = {"n_index_texts": len(index_texts), "auc_all": label_auc(probe), "auc_ids_agree": label_auc(probe[agree]),
               "n_ids_agree": int(agree.sum()),
               "mean_cosine_by_label": {k: float(v) for k, v in probe.groupby("label")["cosine"].mean().items()}}
    return probe, summary


# ---------------------------------------------------------------------------- MLflow

def tracking_uri() -> str:
    """Same rule as s06_train_models.py."""
    return os.environ.get("MLFLOW_TRACKING_URI") or f"sqlite:///{ROOT / 'mlflow.db'}"


def training_runs(run_ids: dict[str, str], uri: str) -> dict[str, dict]:
    """Logged metrics and params of each model's training run. Empty for a run that cannot be read."""
    from mlflow.tracking import MlflowClient
    client = MlflowClient(uri)
    out = {}
    for name, rid in run_ids.items():
        try:
            data = client.get_run(rid).data
            out[name] = {"metrics": dict(data.metrics), "params": dict(data.params)}
        except Exception as e:  # a missing run only blanks D2/D5/D9/D10 for that model
            print(f"warning: cannot read training run {rid} of {name}: {e}")
            out[name] = {"metrics": {}, "params": {}}
    return out


def metric_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-./ ]", "_", name)


def clean_metrics(metrics: dict) -> dict[str, float]:
    return {metric_name(k): float(v) for k, v in metrics.items()
            if v is not None and isinstance(v, (int, float, np.number)) and math.isfinite(float(v))}


def log_to_mlflow(uri: str, tags: dict, parent: dict, children: dict[str, dict],
                  child_tags: dict[str, dict], artifact_dir: Path | None = None) -> str:
    import mlflow
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name="training_diag") as run:
        mlflow.set_tags({**tags, "track": "diag"})
        mlflow.log_metrics(clean_metrics(parent))
        if artifact_dir is not None:
            mlflow.log_artifacts(str(artifact_dir), artifact_path="diag")
        for name, metrics in children.items():
            with mlflow.start_run(run_name=f"diag_{name}", nested=True):
                mlflow.set_tags({**tags, "track": "diag", "model_name": name, **child_tags.get(name, {})})
                mlflow.log_metrics(clean_metrics(metrics))
        return run.info.run_id


# ---------------------------------------------------------------------------- report

def fmt(v, digits=4) -> str:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "n/a"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def results_md(s: dict) -> str:
    d1, d2, d3, d5, d8 = s["d1"], s["d2"], s["d3"], s["d5"], s["d8"]
    lines = [
        "# Training diagnostics", "",
        f"Generated by `pipeline/diag_training_issues.py` (diag_version {DIAG_VERSION}). Feature table sha256 "
        f"`{s['tags']['training_data_sha256'][:12]}`, constants `{s['tags']['constants_sha256'][:12]}`, "
        f"snapshot {s['tags']['snapshot_date']}. Ordered by expected impact on hand-sheet precision.", "",
        "## 1. Match-kind shortcut (D1, D2)", "",
        f"- Train rows whose match kind has a single label: **{fmt(d1['share_train_rows_pure_kind'])}**.",
        f"- All-MATCH kinds: {', '.join(sorted(d1['pure_match_kinds'])) or 'none'}.",
        f"- All-NON_MATCH kinds: {', '.join(sorted(d1['pure_non_match_kinds'])) or 'none'}.",
        f"- Mixed kinds: {', '.join(sorted(d1['mixed_kinds'])) or 'none'}.",
        f"- Rule 'MATCH iff all-MATCH kind' on the label-set test split: precision {fmt(d2['rule']['precision'])}, "
        f"recall {fmt(d2['rule']['recall'])}, accuracy {fmt(d2['rule']['accuracy'])}.", "",
        "| model | test precision | test recall | test AP | rule precision minus model |", "|---|---|---|---|---|",
    ]
    for name, m in d2["models"].items():
        lines.append(f"| {name} | {fmt(m.get('test_precision'))} | {fmt(m.get('test_recall'))} | "
                     f"{fmt(m.get('test_average_precision'))} | {fmt(m.get('rule_minus_model_precision'))} |")
    lines += ["", "## 2. The no-kind-derived ablation (D3)", "",
              f"Depth-{d3['depth']} trees on the {len(NO_KIND_DERIVED)} `xgb_no_kind_derived` features, scored on the test split:",
              f"- rebuild `feat_match_kind`: accuracy {fmt(d3['feat_match_kind_acc'])} "
              f"(majority class {fmt(d3['feat_match_kind_majority_acc'])})",
              f"- predict the label: accuracy {fmt(d3['label_acc'])} (majority class {fmt(d3['label_majority_acc'])})", "",
              "## 3-4. Placeholder models and INSUFFICIENT_EVIDENCE rows on the hand sheet (D4)", "",
              "Accept rate = share of candidate rows at or above the model's validation cutoff. s08 leaves IE rows out.", "",
              "| model | cutoff | IE accepted | placeholder-kind accepted | NON_MATCH accepted | MATCH accepted |",
              "|---|---|---|---|---|---|"]
    for name, m in s["d4"].items():
        lines.append(f"| {name} | {fmt(m['cutoff'])} | {m['ie_accepted']}/{m['ie_n']} | "
                     f"{m['placeholder_accepted']}/{m['placeholder_n']} | {m['non_match_accepted']}/{m['non_match_n']} | "
                     f"{m['match_accepted']}/{m['match_n']} |")
    lines += ["", "Full breakdown by label, match kind and hard-case reason: `d4_accept_rates.csv`.", "",
              "## 5. Cutoff prior (D5)", "",
              f"Test split MATCH share {fmt(d5.get('test_prevalence'))}; weighted hand-sheet MATCH share "
              f"{fmt(d5['target_prevalence'])}. Precision the test split's TPR/FPR would give at the hand-sheet share:", "",
              "| model | test precision | at hand-sheet prior |", "|---|---|---|"]
    for name, m in d5["models"].items():
        lines.append(f"| {name} | {fmt(m.get('test_precision'))} | {fmt(m.get('prior_corrected_precision'))} |")
    mism = {k: v for k, v in s["d6"]["mismatch"].items() if v}
    lines += ["", "## 6. Hand-feature parity with s04 (D6)", "",
              f"{s['d6']['n_rows']} hand candidate rows rebuilt with `sibling_keys` from `data/sibling_keys.csv`.",
              "Mismatching features: " + (", ".join(f"{k} ({v})" for k, v in mism.items()) if mism else "none") + ".",
              "Caveat: the cache holds only keys the label set needed, so a rebuilt False can be a cache miss.", "",
              "## 7. Match kinds never seen in training (D7)", "",
              f"Hand candidate rows with an unseen kind: {s['d7']['n_unseen']}.", "",
              "## 8. Hand labels vs the rule tree (D8)", "",
              f"- Decided candidate rows where the hand label equals the s07 rule tree: {fmt(d8['agree_rate'])} "
              f"({d8['n_decided'] - d8['n_disagree']}/{d8['n_decided']}).",
              f"- IE rows the rule tree would accept: {fmt(d8['ie_rules_accept_rate'])} of {d8['n_ie']}.",
              "- The sheet does not mark formula-filled labels (`labeler` is the same on every row), so human-only "
              "rows cannot be separated; the disagreement rows are the ones where a person added information.", "",
              "| model | accepted on disagreement rows | precision there |", "|---|---|---|"]
    for name, m in d8["models"].items():
        lines.append(f"| {name} | {m['accepted']} | {fmt(m['precision'])} |")
    lines += ["", "## 9. Smaller items (D9)", "",
              "| model | val AP minus test AP |", "|---|---|"]
    for name, gap in s["d9"].items():
        lines.append(f"| {name} | {fmt(gap, 6)} |")
    d10, d11 = s.get("d10", {}), s.get("d11", {})
    lines += ["", "## 10. What the models learn (D10)", "",
              "Each model is refit from its logged params on the s06 fit rows. Contributions are XGBoost SHAP "
              "(`pred_contribs`) or, for LR, coef x (value - fit mean). `fp push` is the mean contribution on the "
              "hand false positives (positive = toward MATCH). Full tables: `d10_shap_<model>.csv`.", ""]
    for name, m in d10.get("models", {}).items():
        lines += [f"**{name}**: reproduced={m['reproduced']} (max |diff| {fmt(m['repro_max_abs_diff'], 6)}), "
                  f"{m['n_false_pos']} hand false positives.", "",
                  "| feature | test mean abs | hand mean abs | fp push |", "|---|---|---|---|"]
        lines += [f"| {r['feature']} | {fmt(r['test_mean_abs'], 3)} | {fmt(r['hand_mean_abs'], 3)} | "
                  f"{fmt(r['false_pos_mean_push'], 3)} |" for r in m["top"]]
        lines.append("")
    if not d10.get("models"):
        lines += ["Not run (needs the training runs' params from MLflow).", ""]
    lines += ["## 11. Embedding probe (D11)", ""]
    if d11:
        lines += [f"TF-IDF character 3-5-grams fitted on {d11['n_index_texts']} GUDID texts of the label set. "
                  "Cosine between the MAUDE text and the candidate text of each hand pair:", "",
                  f"- AUC MATCH vs NON_MATCH, all decided rows: {fmt(d11['auc_all'])}",
                  f"- AUC where the identifiers already agree ({d11['n_ids_agree']} rows): {fmt(d11['auc_ids_agree'])}",
                  "- mean cosine by label: " + ", ".join(f"{k} {fmt(v, 3)}" for k, v in d11["mean_cosine_by_label"].items()),
                  "", "Per-pair texts and cosines: `d11_embedding_probe.csv`.", ""]
    else:
        lines += ["Not run.", ""]
    lines += ["## Notes", "", *[f"- {note}" for note in s["notes"]], ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--features", type=Path, default=ROOT / "data/label_set_v1_features.parquet")
    p.add_argument("--label-set", type=Path, default=ROOT / "data/label_set_v1.parquet",
                   help="key_* columns, for the D11 embedding probe's GUDID texts (D11 skipped if absent)")
    p.add_argument("--sheet", type=Path, default=ROOT / "labels/hand_label_sample_v1.csv")
    p.add_argument("--hand-features", type=Path, default=ROOT / "data/hand_features.csv")
    p.add_argument("--scores-dir", type=Path, default=ROOT / "data/scores")
    p.add_argument("--cutoffs", type=Path, default=ROOT / "data/cutoffs.json")
    p.add_argument("--siblings", type=Path, default=ROOT / "data/sibling_keys.csv")
    p.add_argument("--alias", type=Path, default=ROOT / "labels/alias_table_v1.csv")
    p.add_argument("--manifest", type=Path, default=ROOT / "labels/label_set_v1.manifest.json")
    p.add_argument("--out", type=Path, default=ROOT / "reports/training_diag")
    p.add_argument("--no-mlflow", action="store_true", help="neither read training runs nor log a diag run")
    p.add_argument("--log-artifacts", action="store_true",
                   help="also upload the report files to the run (the experiment's artifact root is GCS)")
    return p.parse_args(argv)


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, dtype=str)


def main(argv=None) -> int:
    args = parse_args(argv)
    inputs = [args.features, args.sheet, args.hand_features, args.cutoffs, args.siblings, args.alias]
    missing = [str(p) for p in inputs if not p.exists()]
    if missing:
        print(f"missing inputs: {missing}")
        return EXIT_CONFIG
    args.out.mkdir(parents=True, exist_ok=True)

    feats = read_table(args.features)
    sheet = pd.read_csv(args.sheet, dtype=str)
    hand_feats = pd.read_csv(args.hand_features, dtype=str)
    cutoffs = json.loads(args.cutoffs.read_text())
    manifest = json.loads(args.manifest.read_text()) if args.manifest.exists() else {}
    cands = hand_candidates(sheet)

    scores, run_ids = {}, {}
    for name in MODELS:
        path = args.scores_dir / f"{name}_hand_scores.csv"
        if path.exists():
            scores[name] = pd.read_csv(path, dtype={"pair_id": str, "mlflow_run_id": str})
            rid = scores[name]["mlflow_run_id"].iloc[0] if len(scores[name]) else "none"
            if rid and rid != "none":
                run_ids[name] = rid
    runs = {} if args.no_mlflow else training_runs(run_ids, tracking_uri())
    trained = {name: r["metrics"] for name, r in runs.items()}
    notes = []
    if args.no_mlflow:
        notes.append("Run with --no-mlflow: training runs were not read, so D2 model columns, D5, D9 and D10 are n/a.")

    # D1-D3
    purity = kind_purity(feats)
    purity.to_csv(args.out / "d1_kind_purity.csv", index=False)
    match_kinds, non_match_kinds = pure_kinds(purity)
    train_kinds = set(purity.loc[purity["meta_split"] == "train", "feat_match_kind"])
    d1 = {"share_train_rows_pure_kind": share_in_pure_kinds(purity), "pure_match_kinds": sorted(match_kinds),
          "pure_non_match_kinds": sorted(non_match_kinds), "mixed_kinds": sorted(train_kinds - match_kinds - non_match_kinds)}
    rule = rule_baseline(feats, match_kinds)
    d2 = {"rule": rule, "models": {}}
    for name in MODELS:
        m = trained.get(name, {})
        d2["models"][name] = {k: m.get(k) for k in ("test_precision", "test_recall", "test_average_precision")}
        d2["models"][name]["rule_minus_model_precision"] = (
            rule["precision"] - m["test_precision"] if m.get("test_precision") is not None else None)
    d3 = shortcut_trees(feats)

    # D4-D5
    d4, d4_tables, d5 = {}, [], {"target_prevalence": hand_prevalence(cands), "models": {}}
    test = feats[feats["meta_split"] == "test"]
    d5["test_prevalence"] = float((test["label"] == "MATCH").mean()) if len(test) else float("nan")
    scored = {}
    for name, sc in scores.items():
        cutoff = cutoffs.get(name)
        if cutoff is None:
            notes.append(f"{name} has no validation cutoff; D4 and D8 skip it.")
            continue
        c = cands.merge(sc[["pair_id", "score"]], on="pair_id", how="inner")
        if len(c) != len(cands):
            notes.append(f"{name}: {len(cands) - len(c)} candidate rows have no score.")
        scored[name] = (c, cutoff)
        t = accept_rates(c, cutoff).assign(model=name, cutoff=cutoff)
        d4_tables.append(t)
        d4[name] = {"cutoff": cutoff}
        for key, (by, value) in {"ie": ("label", IE), "match": ("label", "MATCH"),
                                 "non_match": ("label", "NON_MATCH"),
                                 "placeholder": ("cand_match_kind", PLACEHOLDER_KIND)}.items():
            row = t[(t["by"] == by) & (t["value"] == value)]
            d4[name][f"{key}_accepted"] = int(row["accepted"].sum())
            d4[name][f"{key}_n"] = int(row["n"].sum())
    for name in MODELS:
        m = trained.get(name, {})
        d5["models"][name] = {"test_precision": m.get("test_precision"), "prior_corrected_precision":
                              prior_corrected_precision(m.get("test_precision"), m.get("test_recall"),
                                                        m.get("test_match_rate"), d5["target_prevalence"])}
    if d4_tables:
        pd.concat(d4_tables).to_csv(args.out / "d4_accept_rates.csv", index=False)

    # D6-D7
    rebuilt = rebuild_hand_features(cands, load_aliases(args.alias), read_siblings(args.siblings))
    par = parity(hand_feats, rebuilt)
    par.to_csv(args.out / "d6_parity.csv", index=False)
    unseen = unseen_kinds(hand_feats, train_kinds)
    unseen.to_csv(args.out / "d7_unseen_kinds.csv", index=False)

    # D8: the rule tree, run through s07 itself so its logic is not copied here
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "rules_hand_scores.csv"
        subprocess.run([sys.executable, str(ROOT / "pipeline/s07_rules_baseline.py"),
                        "--features", str(args.hand_features), "--out", str(out)],
                       check=True, capture_output=True, text=True)
        rules = pd.read_csv(out, dtype={"pair_id": str})
    agreement, d8 = rules_agreement(cands, rules)
    agreement[["pair_id", "maude_row_id", "cand_rank", "label", "cand_match_kind", "hard_case_reason",
               "rules_score", "rules_agree"]].to_csv(args.out / "d8_rules_agreement.csv", index=False)
    disagree = agreement.loc[agreement["rules_agree"] == 0, "pair_id"]
    d8["models"] = {}
    for name, (c, cutoff) in scored.items():
        p, n = precision_on(c[c["pair_id"].isin(disagree)], cutoff)
        d8["models"][name] = {"precision": p, "accepted": n}

    # D9
    d9 = {}
    for name in MODELS:
        m = trained.get(name, {})
        if m.get("val_average_precision") is not None and m.get("test_average_precision") is not None:
            d9[name] = m["val_average_precision"] - m["test_average_precision"]
    caps = manifest.get("caps_and_ratios", {})
    if caps.get("easy_anchor_pct") not in (None, 10):
        notes.append(f"label_set_v1 was built with easy_anchor_pct={caps['easy_anchor_pct']}; the s01 default is 10, "
                     "so a rebuild with defaults gives a different label set.")
    # D10
    params = {n: r["params"] for n, r in runs.items() if r["params"]}
    explained = explain_models(feats, hand_feats, params, cutoffs, scores) if params else {}
    d10 = {"reproduced": bool(explained) and all(e["reproduced"] for e in explained.values()), "models": {}}
    for name, e in explained.items():
        e["table"].to_csv(args.out / f"d10_shap_{name}.csv")
        d10["models"][name] = {"repro_max_abs_diff": e["repro_max_abs_diff"], "reproduced": e["reproduced"],
                               "n_false_pos": e["n_false_pos"],
                               "top": e["table"].head(8).reset_index().to_dict("records")}
    if explained and not d10["reproduced"]:
        notes.append("D10: a refit did not reproduce the saved hand scores; do not trust its SHAP table.")

    # D11
    d11 = {}
    if args.label_set.exists():
        cols = ["key_g_mfr", "key_g_name", "key_g_model_raw", "key_g_catalog_raw", "key_g_pcs"]
        probe, d11 = embedding_probe(cands, gudid_side_texts(pd.read_parquet(args.label_set, columns=cols)))
        probe.to_csv(args.out / "d11_embedding_probe.csv", index=False)
    else:
        notes.append(f"D11 skipped: {args.label_set} not found.")
    notes.append("D11 uses TF-IDF only; sentence-transformers models were not run (no downloads in this check).")

    notes.append("s08_evaluate_hand.py hard-codes the sqlite tracking URI and ignores MLFLOW_TRACKING_URI.")
    notes.append("s06 picks hyperparameters and the cutoff on the same validation fold.")

    tags = {"diag_version": DIAG_VERSION,
            "training_data_sha256": hashlib.sha256(args.features.read_bytes()).hexdigest(),
            "constants_sha256": manifest.get("constants_sha256", "unknown"),
            "alias_table_sha256": manifest.get("alias_table_sha256", "unknown"),
            "snapshot_date": manifest.get("snapshot_date", "unknown"),
            "d10_reproduced": str(d10["reproduced"]).lower()}
    summary = {"tags": tags, "d1": d1, "d2": d2, "d3": d3, "d4": d4, "d5": d5,
               "d6": {"n_rows": len(rebuilt), "mismatch": dict(zip(par["feature"], par["n_mismatch"].astype(int)))},
               "d7": {"n_unseen": len(unseen), "kinds": sorted(set(unseen["feat_match_kind"]))},
               "d8": d8, "d9": d9, "d10": d10, "d11": d11, "notes": notes}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    (args.out / "RESULTS.md").write_text(results_md(summary), encoding="utf-8")
    print(f"wrote {args.out}")

    if args.no_mlflow:
        return EXIT_OK
    p_train = purity[purity["meta_split"] == "train"]
    parent = {"d1_share_train_rows_pure_kind": d1["share_train_rows_pure_kind"],
              **{f"d1_kind_purity_{k}": v for k, v in zip(p_train["feat_match_kind"], p_train["purity"])},
              "d2_rule_precision": rule["precision"], "d2_rule_recall": rule["recall"],
              "d3_kind_reconstruction_acc": d3["feat_match_kind_acc"], "d3_label_acc_no_kind_derived": d3["label_acc"],
              "d5_target_prevalence": d5["target_prevalence"],
              **{f"d6_parity_mismatch_{k}": v for k, v in summary["d6"]["mismatch"].items()},
              "d7_unseen_kind_rows": len(unseen),
              "d8_hand_rules_agree_rate": d8["agree_rate"], "d8_ie_rules_accept_rate": d8["ie_rules_accept_rate"],
              "d11_auc_all": d11.get("auc_all"), "d11_auc_ids_agree": d11.get("auc_ids_agree"),
              **{f"d11_mean_cosine_{k}": v for k, v in d11.get("mean_cosine_by_label", {}).items()}}
    children, child_tags = {}, {}
    for name in MODELS:
        m = {"d2_rule_minus_model_precision": d2["models"][name]["rule_minus_model_precision"],
             "d5_prior_corrected_precision": d5["models"][name]["prior_corrected_precision"],
             "d9_val_test_ap_gap": d9.get(name)}
        t = next((x for x in d4_tables if x["model"].iloc[0] == name), None)
        if t is not None:
            for _, r in t[t["by"].isin(["label", "cand_match_kind"])].iterrows():
                prefix = "label" if r["by"] == "label" else "kind"
                m[f"d4_accept_rate_{prefix}_{r['value']}"] = r["accept_rate"]
                m[f"d4_accept_rate_weighted_{prefix}_{r['value']}"] = r["accept_rate_weighted"]
        if name in d8["models"]:
            m["d8_precision_on_rule_disagreements"] = d8["models"][name]["precision"]
            m["d8_accepted_on_rule_disagreements"] = d8["models"][name]["accepted"]
        if name in explained:
            table = explained[name]["table"]
            m["d10_repro_max_abs_diff"] = explained[name]["repro_max_abs_diff"]
            m.update({f"d10_shap_hand_{f}": v for f, v in table["hand_mean_abs"].items()})
            m.update({f"d10_fp_push_{f}": v for f, v in table["false_pos_mean_push"].items()})
        children[name] = m
        child_tags[name] = {"diag_of_run_id": run_ids.get(name, "none")}
    run_id = log_to_mlflow(tracking_uri(), tags, parent, children, child_tags,
                           args.out if args.log_artifacts else None)
    print(f"logged diag run {run_id} to {tracking_uri()}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
