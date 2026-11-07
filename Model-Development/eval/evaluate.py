import mlflow
import pandas as pd
from sklearn.metrics import precision_score, recall_score, f1_score
from sklearn.metrics import precision_recall_curve

# --- run settings (change these per run) ---
TRACK = "test"                 # baseline, xgb, embed or ft for real runs
THRESHOLD_VERSION = "t1"
NORMALIZATION_VERSION = "n1"
SLICE_COLUMN = "has_model_number"

labels = pd.read_csv("fake_labels.csv")
scores = pd.read_csv("fake_scores.csv")

# join scores to labels using pair_id
df = labels.merge(scores, on="pair_id", how="inner")

# safety check: every labeled pair must have a score
assert len(df) == len(labels), "Some pairs have no score!"

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

    for value, group in df.groupby(SLICE_COLUMN):
        p = precision_score(group["label"], group["pred_99"], zero_division=0)
        r = recall_score(group["label"], group["pred_99"], zero_division=0)
        n_matches = int(group["label"].sum())
        n_accepted = int(group["pred_99"].sum())

        metrics[f"precision_slice_{SLICE_COLUMN}_{value}"] = p
        metrics[f"recall_slice_{SLICE_COLUMN}_{value}"] = r
        metrics[f"accepted_slice_{SLICE_COLUMN}_{value}"] = n_accepted
        print(f"{SLICE_COLUMN}={value}: pairs={len(group)}, matches={n_matches}, "
              f"accepted={n_accepted}, precision={p:.4f}, recall={r:.4f}")

# print everything
for name, value in metrics.items():
    print(f"{name}: {value:.4f}")

# --- save to MLflow ---
mlflow.set_tracking_uri("sqlite:///mlflow.db")
mlflow.set_experiment("vigil-md-linkage")

with mlflow.start_run(run_name=model_name):
    mlflow.set_tags({
        "track": TRACK,
        "model_name": model_name,
        "threshold_version": THRESHOLD_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
    })
    mlflow.log_metrics({name: float(value) for name, value in metrics.items()})

print("Logged to MLflow")