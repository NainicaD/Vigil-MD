"""Train logistic regression and XGBoost on the label_set_v1 feature table.

Follows the team's instructions (status, 7 Nov):
- train only on meta_split = train; validation inside train is grouped by meta_device_key
- models see feat_* columns only (FEATURE_COLUMNS from same_device_features.py)
- XGBoost runs three times: all features, without feat_match_kind, and without every
  match-kind-derived feature (the shortcut stays in through those otherwise)
- the cutoff for 99% precision is chosen on validation and saved, then used unchanged on the hand labels
- MLflow logs constants_sha256, the alias hash and the snapshot date from the label manifest

Input: a feature table with label (MATCH/NON_MATCH), meta_split, meta_device_key and the feat_* columns.
Output per model: <model>_hand_scores.csv (scores for the hand sheet) and its cutoff in cutoffs.json.

Usage (from Model-Development/models):
    python train_models.py --data label_set_v1_features.parquet --hand ../eval/hand_features.csv
"""
import argparse
import json
import sys

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import GroupShuffleSplit
from xgboost import XGBClassifier

sys.path.append("../scripts")
from same_device_features import FEATURE_COLUMNS, MatchKind, assert_no_leakage  # noqa: E402

parser = argparse.ArgumentParser(description="Train LR and XGBoost on label_set_v1 features")
parser.add_argument("--data", required=True, help="feature table (.parquet or .csv)")
parser.add_argument("--hand", default="../eval/hand_features.csv", help="hand-sheet features to score")
parser.add_argument("--manifest", default="../labels/label_set_v1.manifest.json")
parser.add_argument("--trials", type=int, default=10, help="random-search trials per XGBoost variant")
parser.add_argument("--no-mlflow", action="store_true")
args = parser.parse_args()

KIND_DERIVED = ["feat_match_kind", "feat_identifier_exact", "feat_identifier_cross_field", "feat_prefix_related"]
VARIANTS = {
    "xgb_all": list(FEATURE_COLUMNS),
    "xgb_no_match_kind": [c for c in FEATURE_COLUMNS if c != "feat_match_kind"],
    "xgb_no_kind_derived": [c for c in FEATURE_COLUMNS if c not in KIND_DERIVED],
}
assert_no_leakage(FEATURE_COLUMNS)


# --- loading and encoding --------------------------------------------------------------------------
def read_table(path):
    return pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path, dtype=str)


def to_float(series):
    """True/False/number/empty -> 1.0/0.0/number/NaN, whether it arrived as text or as bool."""
    text = series.astype(str).str.strip().str.upper()
    mapped = text.map({"TRUE": 1.0, "FALSE": 0.0})
    numeric = pd.to_numeric(series, errors="coerce")
    return mapped.fillna(numeric)


def encode(df):
    """feat_* columns as numbers; feat_match_kind as a category with a fixed list of values."""
    X = pd.DataFrame(index=df.index)
    for col in FEATURE_COLUMNS:
        if col == "feat_match_kind":
            X[col] = pd.Categorical(df[col], categories=[k.value for k in MatchKind])
        else:
            X[col] = to_float(df[col])
    return X


def lr_matrix(X):
    """Logistic regression needs no gaps and no categories: one-hot the kind, flag missing values,
    log the DI count, and add the feature cross the plan asks for (identifier AND labeler agree)."""
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


def recall_at_p99(y, scores):
    """Lowest cutoff with precision >= 0.99 on validation, and recall there."""
    precision, recall, thresholds = precision_recall_curve(y, scores)
    precision, recall = precision[:-1], recall[:-1]
    ok = precision >= 0.99
    if not ok.any():
        return None, 0.0
    best = recall[ok].argmax()
    return float(thresholds[ok][best]), float(recall[ok][best])


data = read_table(args.data)
train = data[data["meta_split"] == "train"].reset_index(drop=True)
y = (train["label"] == "MATCH").astype(int).to_numpy()
X = encode(train)
print(f"train rows: {len(train)} (of {len(data)}), match rate {y.mean():.1%}")

# validation inside train, grouped by device so no device sits on both sides
splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
fit_idx, val_idx = next(splitter.split(X, y, groups=train["meta_device_key"]))
assert set(train.loc[fit_idx, "meta_device_key"]).isdisjoint(train.loc[val_idx, "meta_device_key"])
print(f"fit rows: {len(fit_idx)}, validation rows: {len(val_idx)}, validation match rate {y[val_idx].mean():.1%}")

hand = read_table(args.hand)
X_hand = encode(hand)

manifest = {}
try:
    with open(args.manifest) as f:
        manifest = json.load(f)
except FileNotFoundError:
    print(f"warning: {args.manifest} not found; hashes will be logged as unknown")

if not args.no_mlflow:
    import mlflow
    import mlflow.xgboost
    mlflow.set_tracking_uri("sqlite:///../mlflow.db")
    mlflow.set_experiment("vigil-md-linkage")

common_tags = {
    "threshold_version": "t1", "normalization_version": "n1", "data_file": args.data,
    "constants_sha256": manifest.get("constants_sha256", "unknown"),
    "alias_table_sha256": manifest.get("alias_table_sha256", "unknown"),
    "snapshot_date": manifest.get("snapshot_date", "unknown"),
}
cutoffs = {}


def finish(name, track, val_scores, hand_scores, params, model=None):
    """Pick the cutoff on validation, log everything, write the hand-sheet scores file."""
    cutoff, recall = recall_at_p99(y[val_idx], val_scores)
    avg_prec = average_precision_score(y[val_idx], val_scores)
    cutoffs[name] = cutoff
    print(f"  {name}: validation avg_precision={avg_prec:.4f}, recall_at_p99={recall:.4f}, cutoff={cutoff}")

    run_id = "none"
    if not args.no_mlflow:
        with mlflow.start_run(run_name=name) as run:
            mlflow.set_tags({**common_tags, "track": track, "model_name": name})
            mlflow.log_params(params)
            mlflow.log_metrics({"val_average_precision": avg_prec, "val_recall_at_p99": recall,
                                "val_match_rate": float(y[val_idx].mean())})
            if cutoff is not None:
                mlflow.log_metric("cutoff_p99", cutoff)
            if model is not None:
                mlflow.xgboost.log_model(model, name="model")
            run_id = run.info.run_id

    pd.DataFrame({
        "pair_id": hand["pair_id"], "score": np.round(hand_scores, 6), "model_name": name,
        "mlflow_run_id": run_id, "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
    }).to_csv(f"{name}_hand_scores.csv", index=False)


# --- logistic regression baseline --------------------------------------------------------------------
print("\nlogistic regression")
M = lr_matrix(X)
M_hand = lr_matrix(X_hand).reindex(columns=M.columns, fill_value=0.0)
lr = LogisticRegression(max_iter=2000)
lr.fit(M.iloc[fit_idx], y[fit_idx])
finish("logreg_v1", "baseline", lr.predict_proba(M.iloc[val_idx])[:, 1],
       lr.predict_proba(M_hand)[:, 1], {"model": "LogisticRegression", "max_iter": 2000})

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
        model.fit(X.iloc[fit_idx][cols], y[fit_idx])
        val_scores = model.predict_proba(X.iloc[val_idx][cols])[:, 1]
        avg_prec = average_precision_score(y[val_idx], val_scores)
        print(f"  trial {trial:02d}: avg_precision={avg_prec:.4f}")
        if best is None or avg_prec > best["avg_prec"]:
            best = {"avg_prec": avg_prec, "model": model, "params": params, "val": val_scores}
    hand_scores = best["model"].predict_proba(X_hand[cols])[:, 1]
    finish(name, "xgb", best["val"], hand_scores, {**best["params"], "features": ",".join(cols)}, best["model"])

with open("cutoffs.json", "w") as f:
    json.dump(cutoffs, f, indent=2)

print("\nNext, score each model on the hand labels with its validation cutoff:")
for name, cutoff in cutoffs.items():
    track = "baseline" if name.startswith("logreg") else "xgb"
    if cutoff is None:
        print(f"  {name}: never reached 99% precision on validation, so no cutoff")
        continue
    print(f"  cd ../eval && python evaluate_hand.py --scores ../models/{name}_hand_scores.csv "
          f"--cutoff {cutoff:.6f} --track {track}")
