import argparse

import mlflow
import pandas as pd
from sklearn.metrics import precision_score, recall_score, f1_score
from sklearn.metrics import precision_recall_curve

# --- read options from the command line ---
parser = argparse.ArgumentParser(description="Score a model with the shared eval script")
parser.add_argument("--labels", default="fake_labels.csv", help="labels file (pair_id, label, slice columns)")
parser.add_argument("--scores", default="fake_scores.csv", help="scores file written by a model")
parser.add_argument("--track", default="test", choices=["test", "baseline", "xgb", "embed", "ft"])
parser.add_argument("--slice", default="has_model_number", help="column to split results by")
parser.add_argument("--threshold-version", default="t1")
parser.add_argument("--normalization-version", default="n1")
parser.add_argument("--no-mlflow", action="store_true", help="print results without logging")
args = parser.parse_args()

labels = pd.read_csv(args.labels)
scores = pd.read_csv(args.scores)

# join scores to labels using pair_id
df = labels.merge(scores, on="pair_id", how="inner")

# safety checks
assert len(df) == len(labels), "Some pairs have no score!"
assert args.slice in df.columns, f"Slice column '{args.slice}' not found in labels file"

model_name = df["model_name"].iloc[0]
metrics = {}   # every number we want to save goes in here

# --- metrics at a fixed cutoff ---
df["pred"] = (df["score"] >= 0.5).astype(int)
metrics["precision_at_0.5"] = precision_score(df["label"], df["pred"])
metrics["recall_at_0.5"] = recall_score(df["label"], df["pred"])
metrics["f1_at_0.5"] = f1_score(df["label"], df["pred"])

# --- recall at 99% precision ---
precision, recall, thresholds = precision_recall_curve(df["label"], df["score"])
precision = precision[:-1]
recall = recall[:-1]
ok = precision >= 0.99

if ok.any():
    best = recall[ok].argmax()
    cutoff = thresholds[ok][best]
    metrics["cutoff_p99"] = cutoff
    metrics["recall_at_p99"] = recall[ok][best]
else:
    cutoff = None
    print("This model never reaches 99% precision")

# --- precision by slice (same cutoff for every group) ---
if cutoff is not None:
    df["pred_99"] = (df["score"] >= cutoff).astype(int)

    for value, group in df.groupby(args.slice):
        p = precision_score(group["label"], group["pred_99"], zero_division=0)
        r = recall_score(group["label"], group["pred_99"], zero_division=0)
        n_matches = int(group["label"].sum())
        n_accepted = int(group["pred_99"].sum())

        metrics[f"precision_slice_{args.slice}_{value}"] = p
        metrics[f"recall_slice_{args.slice}_{value}"] = r
        metrics[f"accepted_slice_{args.slice}_{value}"] = n_accepted
        print(f"{args.slice}={value}: pairs={len(group)}, matches={n_matches}, "
              f"accepted={n_accepted}, precision={p:.4f}, recall={r:.4f}")

# print everything
print(f"\nmodel: {model_name}  track: {args.track}")
for name, value in metrics.items():
    print(f"{name}: {value:.4f}")

# --- save to MLflow ---
if args.no_mlflow:
    print("Skipped MLflow logging")
else:
    mlflow.set_tracking_uri("sqlite:///../mlflow.db")
    mlflow.set_experiment("vigil-md-linkage")

    with mlflow.start_run(run_name=model_name):
        mlflow.set_tags({
            "track": args.track,
            "model_name": model_name,
            "threshold_version": args.threshold_version,
            "normalization_version": args.normalization_version,
        })
        mlflow.log_params({"labels_file": args.labels, "scores_file": args.scores})
        mlflow.log_metrics({name: float(value) for name, value in metrics.items()})

    print("Logged to MLflow")