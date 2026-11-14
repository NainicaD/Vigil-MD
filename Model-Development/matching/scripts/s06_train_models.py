"""Train logistic regression and XGBoost on the label_set_v1 feature table.

Follows the team's instructions (status, 7 Nov):
- train only on meta_split = train; validation inside train is grouped by meta_device_key
- models see feat_* columns only: the feature set the table holds (FEATURE_SETS in same_device_features.py)
- XGBoost runs three times: all features, without feat_match_kind, and without every
  match-kind-derived feature (the shortcut stays in through those otherwise)
- validation is split again by device: one half picks the XGBoost trial, the other half picks the
  cutoff for 99% precision, which is saved and then used unchanged on the hand labels
- MLflow logs constants_sha256, the alias hash and the snapshot date from the label manifest

Input: a feature table with label (MATCH/NON_MATCH), meta_split, meta_device_key and the feat_* columns.
Output per model: <model>_hand_scores.csv (scores for the hand sheet) and its cutoff in cutoffs.json,
in data/runs/<exp>/ when --exp or $VIGIL_EXP is set, else in data/scores/ and data/cutoffs.json (CI).

Usage (from anywhere):
    python scripts/s06_train_models.py
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import GroupShuffleSplit
from xgboost import XGBClassifier

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model.same_device_features import (  # noqa: E402
    MatchKind, assert_no_leakage, feature_columns_in, feature_set_name, rule_constants_sha256,
)
from vigil_model.tracking import lineage_tags, run_outputs, tracking_uri  # noqa: E402

parser = argparse.ArgumentParser(description="Train LR and XGBoost on label_set_v1 features")
parser.add_argument("--data", default=ROOT / "data/label_set_v1_features.parquet",
                    help="feature table from s04 (.parquet or .csv)")
parser.add_argument("--hand", default=ROOT / "data/hand_features.csv", help="hand-sheet features to score")
parser.add_argument("--manifest", default=ROOT / "labels/label_set_v1.manifest.json")
parser.add_argument("--trials", type=int, default=10, help="random-search trials per XGBoost variant")
parser.add_argument("--exp", help="experiment id from docs/EXPERIMENTS.md (default: $VIGIL_EXP)")
parser.add_argument("--no-mlflow", action="store_true")
parser.add_argument("--sample-weight", metavar="CSV",
                    help="IPW mitigation: the data pipeline's mitigation_weights.csv (see vigil_model/ipw.py)")
args = parser.parse_args()

KIND_DERIVED = ["feat_match_kind", "feat_identifier_exact", "feat_identifier_cross_field", "feat_prefix_related",
                "feat_contains_extra_len", "feat_contains_at_end"]   # the last two are non-null only for contains


# --- loading and encoding --------------------------------------------------------------------------
def read_table(path):
    return pd.read_parquet(path) if str(path).endswith(".parquet") else pd.read_csv(path, dtype=str)


# Where the training data is published. The MLflow server writes run artifacts (the models) under
# <root>/artifacts; see docker-compose.mlflow.yml, which must point at the same root.
GCS_ROOT = os.environ.get("VIGIL_MLFLOW_GCS", "gs://vigil-md-raw-nd/mlflow")


def publish_training_data(path):
    """Copy the feature table and its manifest to GCS under their own content hash, so that a run
    recorded in MLflow names a file anyone on the team can pull and nobody can overwrite.
    Returns (uri, sha256). Uploads nothing if that version is already there."""
    from google.cloud import storage

    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    bucket_name, _, prefix = GCS_ROOT.removeprefix("gs://").partition("/")
    bucket = storage.Client().bucket(bucket_name)
    uri = None
    for f in (path, path.with_suffix(".manifest.json")):
        if not f.exists():
            continue
        blob = bucket.blob(f"{prefix}/data/{path.stem}/{digest[:12]}/{f.name}")
        if not blob.exists():
            blob.upload_from_filename(str(f))
        if f == path:
            uri = f"gs://{bucket_name}/{blob.name}"
    return uri, digest


def to_float(series):
    """True/False/number/empty -> 1.0/0.0/number/NaN, whether it arrived as text or as bool."""
    text = series.astype(str).str.strip().str.upper()
    mapped = text.map({"TRUE": 1.0, "FALSE": 0.0})
    numeric = pd.to_numeric(series, errors="coerce")
    return mapped.fillna(numeric)


def encode(df):
    """feat_* columns as numbers; feat_match_kind as a category with a fixed list of values."""
    X = pd.DataFrame(index=df.index)
    for col in COLS:
        if col == "feat_match_kind":
            X[col] = pd.Categorical(df[col], categories=[k.value for k in MatchKind])
        else:
            X[col] = to_float(df[col])
    return X


def lr_matrix(X):
    """Logistic regression needs no gaps and no categories: one-hot the kind, flag missing values,
    log the DI count, and add the feature cross the plan asks for (identifier AND labeler agree)."""
    M = pd.get_dummies(X["feat_match_kind"], prefix="kind", dtype=float)
    for col in COLS:
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


def recall_at_p99(y, scores):
    """Lowest cutoff with precision >= 0.99 on validation, and recall there."""
    precision, recall, thresholds = precision_recall_curve(y, scores)
    precision, recall = precision[:-1], recall[:-1]
    ok = precision >= 0.99
    if not ok.any():
        return None, 0.0
    best = recall[ok].argmax()
    return float(thresholds[ok][best]), float(recall[ok][best])


def at_cutoff(y_true, scores, cutoff):
    """Precision and recall at a cutoff chosen elsewhere. (None, None) if there is no cutoff."""
    if cutoff is None:
        return None, None
    pred = scores >= cutoff
    tp = int((pred & (y_true == 1)).sum())
    fp = int((pred & (y_true == 0)).sum())
    fn = int(((~pred) & (y_true == 1)).sum())
    return (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)


data = read_table(args.data)
# train on the feature set the table holds (same_device_features.FEATURE_SETS), not the newest one, so a
# table built for an older experiment still trains with today's code
COLS = feature_columns_in(data.columns)
FEATURE_SET = feature_set_name(COLS)
assert_no_leakage(COLS)
VARIANTS = {
    "xgb_all": list(COLS),
    "xgb_no_match_kind": [c for c in COLS if c != "feat_match_kind"],
    "xgb_no_kind_derived": [c for c in COLS if c not in KIND_DERIVED],
}
print(f"feature set {FEATURE_SET}: {len(COLS)} columns")
train = data[data["meta_split"] == "train"].reset_index(drop=True)
y = (train["label"] == "MATCH").astype(int).to_numpy()
X = encode(train)
print(f"train rows: {len(train)} (of {len(data)}), match rate {y.mean():.1%}")
W = None   # sample weights over train rows; None trains unweighted, as before
if args.sample_weight:
    from vigil_model.ipw import row_weights
    W = row_weights(train, args.sample_weight)
    print(f"sample weights from {args.sample_weight}: min {W.min():.3f}, max {W.max():.3f}")

# validation inside train, grouped by device so no device sits on both sides
splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
fit_idx, val_idx = next(splitter.split(X, y, groups=train["meta_device_key"]))
assert set(train.loc[fit_idx, "meta_device_key"]).isdisjoint(train.loc[val_idx, "meta_device_key"])
print(f"fit rows: {len(fit_idx)}, validation rows: {len(val_idx)}, validation match rate {y[val_idx].mean():.1%}")
# trial selection and the cutoff use different devices, so the cutoff is not tuned on the winner's own fold
half = GroupShuffleSplit(n_splits=1, test_size=0.5, random_state=43)
sel_pos, cut_pos = next(half.split(val_idx, groups=train.loc[val_idx, "meta_device_key"]))
sel_idx, cut_idx = val_idx[sel_pos], val_idx[cut_pos]
assert set(train.loc[sel_idx, "meta_device_key"]).isdisjoint(train.loc[cut_idx, "meta_device_key"])
print(f"  validation: {len(sel_idx)} rows pick the trial, {len(cut_idx)} rows pick the cutoff")

# the held-out split of the label set: never fitted, never used to pick the cutoff
test = data[data["meta_split"] == "test"].reset_index(drop=True)
y_test = (test["label"] == "MATCH").astype(int).to_numpy()
X_test = encode(test)
assert set(train["meta_device_key"]).isdisjoint(test["meta_device_key"]), "a device is in both splits"
print(f"test rows: {len(test)}, match rate {y_test.mean():.1%}")

# the bias report found the top-10 manufacturers under-represented in training, so report
# the test split per tier as well as overall. Absent on a feature table built before s04 added it.
TEST_TIERS = ({t: (test["meta_mfr_tier"] == t).to_numpy() for t in sorted(test["meta_mfr_tier"].unique())}
              if "meta_mfr_tier" in test.columns else {})

hand = read_table(args.hand)
missing_hand = [c for c in COLS if c not in hand.columns]
if missing_hand:
    raise SystemExit(f"{args.hand} lacks {missing_hand}: rebuild it with "
                     f"scripts/s05_build_hand_features.py --feature-set {FEATURE_SET}")
X_hand = encode(hand)

manifest = {}
try:
    with open(args.manifest) as f:
        manifest = json.load(f)
except FileNotFoundError:
    print(f"warning: {args.manifest} not found; hashes will be logged as unknown")

# refuse a feature table built under different rules than the label set it claims to describe
feature_manifest = Path(args.data).with_suffix(".manifest.json")
if feature_manifest.exists() and manifest:
    fm = json.loads(feature_manifest.read_text())
    if "rule_constants_sha256" in fm and "constants" in manifest:
        # label rules only: a new feature column changes constants_sha256 but not the labels
        built, labels = fm["rule_constants_sha256"], rule_constants_sha256(manifest["constants"])
    else:
        built, labels = fm.get("constants_sha256"), manifest.get("constants_sha256", "?")
    if built and built != labels:
        raise SystemExit(f"{args.data} was built with rule constants {built[:12]}, but "
                         f"{args.manifest} says {labels[:12]}. "
                         "Rebuild it with scripts/s04_build_feature_table.py.")
    if fm.get("feature_columns") and fm["feature_columns"] != COLS:
        raise SystemExit(f"{args.data} holds {COLS} but its manifest says {fm['feature_columns']}")

data_uri, data_sha256 = "none", "none"
if not args.no_mlflow:
    import mlflow
    import mlflow.xgboost
    mlflow.set_tracking_uri(tracking_uri())
    mlflow.set_experiment("vigil-md-linkage")
    data_uri, data_sha256 = publish_training_data(args.data)
    print(f"training data published: {data_uri}")

common_tags = {
    **lineage_tags(args.manifest, args.exp),
    "feature_set": FEATURE_SET,
    "threshold_version": "t1", "normalization_version": "n1", "data_file": str(args.data),
    "training_data_uri": data_uri, "training_data_sha256": data_sha256,
    "constants_sha256": manifest.get("constants_sha256", "unknown"),
    "alias_table_sha256": manifest.get("alias_table_sha256", "unknown"),
    "snapshot_date": manifest.get("snapshot_date", "unknown"),
    "sample_weight_source": args.sample_weight or "none",
}
cutoffs = {}
train_metrics = {}   # per model: what MLflow gets, also on disk for ci/gate.py (works with --no-mlflow)
trial_rows = []      # every random-search trial, for eval/hparam_sensitivity.py
SCORES, CUTOFFS = run_outputs(args.exp)   # data/runs/<exp>/ with an experiment id, else data/scores
print(f"outputs: {SCORES.relative_to(ROOT)}, {CUTOFFS.relative_to(ROOT)}")


def finish(name, track, val_scores, test_scores, hand_scores, params, model=None):
    """Pick the cutoff on the cutoff half of validation (val_scores are its rows), score the test split."""
    cutoff, recall = recall_at_p99(y[cut_idx], val_scores)
    avg_prec = average_precision_score(y[cut_idx], val_scores)
    cutoffs[name] = cutoff
    print(f"  {name}: validation avg_precision={avg_prec:.4f}, recall_at_p99={recall:.4f}, cutoff={cutoff}")

    # the test split is scored at the validation cutoff, never at one chosen on itself
    test_prec, test_rec = at_cutoff(y_test, test_scores, cutoff)
    test_ap = average_precision_score(y_test, test_scores)
    if cutoff is None:
        print(f"  {name}: no cutoff reached 99% precision, so the test split is unscored")
    else:
        print(f"  {name}: TEST precision={test_prec:.4f}, recall={test_rec:.4f}, avg_precision={test_ap:.4f}")
        for tier, idx in TEST_TIERS.items():
            if y_test[idx].sum() == 0:      # no positives: precision/recall say nothing
                continue
            tp, tr = at_cutoff(y_test[idx], test_scores[idx], cutoff)
            print(f"    {tier}: n={int(idx.sum())}, precision={tp:.4f}, recall={tr:.4f}")

    m = {"val_average_precision": float(avg_prec), "val_recall_at_p99": recall,
         "val_match_rate": float(y[cut_idx].mean()), "test_average_precision": float(test_ap),
         "test_match_rate": float(y_test.mean())}
    if cutoff is not None:
        m.update({"cutoff_p99": cutoff, "test_precision": test_prec, "test_recall": test_rec})
    train_metrics[name] = m

    run_id = "none"
    if not args.no_mlflow:
        with mlflow.start_run(run_name=name) as run:
            mlflow.set_tags({**common_tags, "track": track, "model_name": name})
            mlflow.log_params(params)
            mlflow.log_metrics(m)
            if model is not None:
                # the registry gives each run's model a version number under a stable name;
                # its files live in the run's artifact folder, which is on GCS
                mlflow.xgboost.log_model(model, name="model",
                                         registered_model_name=f"vigil-md-linkage-{name}")
            run_id = run.info.run_id

    SCORES.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "pair_id": hand["pair_id"], "score": np.round(hand_scores, 6), "model_name": name,
        "mlflow_run_id": run_id, "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
    }).to_csv(SCORES / f"{name}_hand_scores.csv", index=False)


# --- logistic regression baseline --------------------------------------------------------------------
print("\nlogistic regression")
M = lr_matrix(X)
M_hand = lr_matrix(X_hand).reindex(columns=M.columns, fill_value=0.0)
M_test = lr_matrix(X_test).reindex(columns=M.columns, fill_value=0.0)
lr = LogisticRegression(max_iter=2000)
lr.fit(M.iloc[fit_idx], y[fit_idx], sample_weight=None if W is None else W[fit_idx])
finish("logreg_v1", "baseline", lr.predict_proba(M.iloc[cut_idx])[:, 1],
       lr.predict_proba(M_test)[:, 1], lr.predict_proba(M_hand)[:, 1],
       {"model": "LogisticRegression", "max_iter": 2000})

# --- XGBoost, three feature variants, random search each -------------------------------------------
rng = np.random.default_rng(42)
for name, cols in VARIANTS.items():
    print(f"\n{name} ({len(cols)} features), {args.trials} trials")
    best = None
    for trial in range(args.trials):
        params = {
            "n_estimators": int(rng.integers(100, 500)),
            "max_depth": int(rng.integers(2, 8)),
            "learning_rate": float(rng.choice([0.03, 0.05, 0.1, 0.2])),
            "subsample": float(rng.uniform(0.6, 1.0)),
            "colsample_bytree": float(rng.uniform(0.6, 1.0)),
            "min_child_weight": int(rng.integers(1, 10)),
        }
        model = XGBClassifier(**params, tree_method="hist", enable_categorical=True, random_state=42, n_jobs=-1)
        model.fit(X.iloc[fit_idx][cols], y[fit_idx], sample_weight=None if W is None else W[fit_idx])
        sel_scores = model.predict_proba(X.iloc[sel_idx][cols])[:, 1]
        avg_prec = average_precision_score(y[sel_idx], sel_scores)
        print(f"  trial {trial:02d}: avg_precision={avg_prec:.4f}")
        trial_rows.append({"model": name, "trial": trial, **params, "val_select_average_precision": avg_prec})
        if best is None or avg_prec > best["avg_prec"]:
            best = {"avg_prec": avg_prec, "model": model, "params": params}
    cut_scores = best["model"].predict_proba(X.iloc[cut_idx][cols])[:, 1]
    hand_scores = best["model"].predict_proba(X_hand[cols])[:, 1]
    test_scores = best["model"].predict_proba(X_test[cols])[:, 1]
    finish(name, "xgb", cut_scores, test_scores, hand_scores,
           {**best["params"], "features": ",".join(cols), "val_select_average_precision": best["avg_prec"]},
           best["model"])

SCORES.mkdir(parents=True, exist_ok=True)
with open(CUTOFFS, "w") as f:
    json.dump(cutoffs, f, indent=2)
(SCORES / "train_metrics.json").write_text(json.dumps(train_metrics, indent=2))
pd.DataFrame(trial_rows).to_csv(SCORES / "trials.csv", index=False)

print("\nNext, score each model on the hand labels with its validation cutoff:")
for name, cutoff in cutoffs.items():
    track = "baseline" if name.startswith("logreg") else "xgb"
    if cutoff is None:
        print(f"  {name}: never reached 99% precision on validation, so no cutoff")
        continue
    print(f"  python eval/s08_evaluate_hand.py --scores {(SCORES / f'{name}_hand_scores.csv').relative_to(ROOT)} "
          f"--cutoff {cutoff:.6f} --track {track}")
