import argparse
import sys

import mlflow
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve
from sklearn.model_selection import GroupShuffleSplit
from xgboost import XGBClassifier

sys.path.append("../features")
from features import FEATURE_NAMES

parser = argparse.ArgumentParser(description="Train XGBoost on the shared features")
parser.add_argument("--data", default="fake_features.csv")
parser.add_argument("--trials", type=int, default=20)
parser.add_argument("--overfit", action="store_true", help="sanity check on 200 pairs")
args = parser.parse_args()


def recall_at_p99(y_true, scores):
    """Lowest cutoff with precision >= 0.99, and recall there (copy of evaluate.py logic)."""
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    precision, recall = precision[:-1], recall[:-1]
    ok = precision >= 0.99
    if not ok.any():
        return None, 0.0
    best = recall[ok].argmax()
    return float(thresholds[ok][best]), float(recall[ok][best])


df = pd.read_csv(args.data)
X = df[FEATURE_NAMES]
y = df["label"]

# --- split by device, so no device is in both train and validation ---
splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=42)
train_idx, val_idx = next(splitter.split(X, y, groups=df["device_id"]))
train_devices = set(df.iloc[train_idx]["device_id"])
val_devices = set(df.iloc[val_idx]["device_id"])
assert train_devices.isdisjoint(val_devices), "A device is in both train and validation!"

X_train, y_train = X.iloc[train_idx], y.iloc[train_idx]
X_val, y_val = X.iloc[val_idx], y.iloc[val_idx]
print(f"train pairs={len(train_idx)}, val pairs={len(val_idx)}")

# --- sanity check: can the model memorize 200 pairs? ---
if args.overfit:
    small_X, small_y = X_train.iloc[:200], y_train.iloc[:200]
    model = XGBClassifier(n_estimators=500, max_depth=8, learning_rate=0.3)
    model.fit(small_X, small_y)
    acc = (model.predict(small_X) == small_y).mean()
    print(f"accuracy on the 200 training pairs: {acc:.3f} (should be close to 1.0)")
    sys.exit()

# --- random search, every trial logged to MLflow ---
# pick the best trial by average precision (stable), report recall at 99% (strict but noisy)
mlflow.set_tracking_uri("sqlite:///../mlflow.db")
mlflow.set_experiment("vigil-md-linkage")
rng = np.random.default_rng(42)
best = None

for trial in range(args.trials):
    params = {
        "n_estimators": int(rng.integers(100, 600)),
        "max_depth": int(rng.integers(2, 8)),
        "learning_rate": float(rng.choice([0.01, 0.03, 0.05, 0.1, 0.2])),
        "subsample": float(rng.uniform(0.6, 1.0)),
        "colsample_bytree": float(rng.uniform(0.6, 1.0)),
        "min_child_weight": int(rng.integers(1, 10)),
    }
    model = XGBClassifier(**params, random_state=42)
    model.fit(X_train, y_train)
    val_scores = model.predict_proba(X_val)[:, 1]

    cutoff, recall = recall_at_p99(y_val, val_scores)
    avg_precision = average_precision_score(y_val, val_scores)

    with mlflow.start_run(run_name=f"xgb_trial_{trial:02d}") as run:
        mlflow.set_tags({"track": "xgb", "model_name": "xgb_v1",
                         "threshold_version": "t1", "normalization_version": "n1"})
        mlflow.log_params({**params, "data_file": args.data})
        mlflow.log_metric("recall_at_p99", recall)
        mlflow.log_metric("average_precision", avg_precision)
        if cutoff is not None:
            mlflow.log_metric("cutoff_p99", cutoff)

    print(f"trial {trial:02d}: avg_precision={avg_precision:.4f}, recall_at_p99={recall:.4f}")
    if best is None or avg_precision > best["avg_precision"]:
        best = {"avg_precision": avg_precision, "recall": recall,
                "run_id": run.info.run_id, "scores": val_scores, "trial": trial}

print(f"\nbest trial: {best['trial']:02d}, avg_precision={best['avg_precision']:.4f}, "
      f"recall_at_p99={best['recall']:.4f}")

# --- write files for the shared eval script ---
val = df.iloc[val_idx]
pd.DataFrame({
    "pair_id": val["pair_id"],
    "score": best["scores"].round(4),
    "model_name": "xgb_v1",
    "mlflow_run_id": best["run_id"],
    "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
}).to_csv("xgb_val_scores.csv", index=False)
val[["pair_id", "label", "maude_has_identifier"]].to_csv("val_labels.csv", index=False)
print("Saved xgb_val_scores.csv and val_labels.csv")