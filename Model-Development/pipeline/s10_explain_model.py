"""Explain a trained XGBoost model with SHAP values (XGBoost's built-in pred_contribs).

For every pair, a SHAP value says how much each feature pushed the score up or down.
This answers WHY the model accepts wrong pairs on the hand labels.

Loads the model saved by s06 from MLflow (no retraining), then:
- average size of each feature's push on a test-split sample and on the hand labels
- average push on the hand-label false positives (the wrong links)
Saves a chart and a table, and attaches both to the model's MLflow run.

Usage (from Model-Development):
    python pipeline/s10_explain_model.py
    python pipeline/s10_explain_model.py --model-name xgb_all --cutoff 0.027015
"""
import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mlflow
import mlflow.xgboost
import pandas as pd
import xgboost as xgb

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

parser = argparse.ArgumentParser(description="SHAP explanation for a trained model")
parser.add_argument("--model-name", default="xgb_no_kind_derived")
parser.add_argument("--cutoff", type=float, default=0.197911, help="the validation cutoff s06 printed")
parser.add_argument("--test-sample", type=int, default=5000)
args = parser.parse_args()


def to_float(series):
    """True/False/number/empty -> 1.0/0.0/number/NaN."""
    text = series.astype(str).str.strip().str.upper()
    return text.map({"TRUE": 1.0, "FALSE": 0.0}).fillna(pd.to_numeric(series, errors="coerce"))


# --- find the training run and load its model ------------------------------------------------------
mlflow.set_tracking_uri(f"sqlite:///{ROOT / 'mlflow.db'}")
runs = mlflow.search_runs(experiment_names=["vigil-md-linkage"],
                          filter_string=f"tags.model_name = '{args.model_name}'",
                          order_by=["start_time DESC"])
# training runs have parameters; the s08 hand-label runs with the same name do not
runs = runs[runs["params.n_estimators"].notna()] if "params.n_estimators" in runs.columns else runs.iloc[0:0]
assert len(runs), f"no training run found for {args.model_name}; run s06 first"
run = runs.iloc[0]
print(f"model: {args.model_name}   run: {run['run_id']}")

try:
    model = mlflow.xgboost.load_model(f"runs:/{run['run_id']}/model")
except Exception:
    # MLflow 3 can store models outside the run; look it up by the run that made it
    found = mlflow.search_logged_models(experiment_ids=[run["experiment_id"]],
                                        filter_string=f"source_run_id = '{run['run_id']}'",
                                        output_format="list")
    model = mlflow.xgboost.load_model(f"models:/{found[0].model_id}")

booster = model.get_booster() if hasattr(model, "get_booster") else model
cols = booster.feature_names
print(f"features: {len(cols)}")


def contributions(df):
    """SHAP value of every feature for every row, plus the model's score."""
    X = pd.DataFrame({c: to_float(df[c]) for c in cols})
    dm = xgb.DMatrix(X, feature_names=cols)
    contrib = booster.predict(dm, pred_contribs=True)   # last column is the bias term
    return pd.DataFrame(contrib[:, :-1], columns=cols), booster.predict(dm)


# --- compute ---------------------------------------------------------------------------------------
hand = pd.read_csv(DATA / "hand_features.csv", dtype=str)
test = pd.read_parquet(DATA / "label_set_v1_features.parquet")
test = test[test["meta_split"] == "test"].sample(n=args.test_sample, random_state=42)

hand_c, hand_score = contributions(hand)
test_c, _ = contributions(test)

wrong = (hand_score >= args.cutoff) & (hand["label"] == "NON_MATCH").to_numpy()
accepted = int((hand_score >= args.cutoff)[hand["label"] != "INSUFFICIENT_EVIDENCE"].sum())
print(f"sanity check vs s08: accepted={accepted}, false positives={int(wrong.sum())}")

summary = pd.DataFrame({
    "test_mean_abs": test_c.abs().mean(),
    "hand_mean_abs": hand_c.abs().mean(),
    "false_pos_mean_push": hand_c[wrong].mean(),
}).sort_values("hand_mean_abs", ascending=False)

print("\nfeature importance (mean |SHAP|) and push on the wrong links:")
print(summary.round(3).to_string())

# --- save and attach to the MLflow run --------------------------------------------------------------
out = DATA / "reports"
out.mkdir(parents=True, exist_ok=True)
table_path = out / f"shap_{args.model_name}.csv"
chart_path = out / f"shap_{args.model_name}.png"
summary.to_csv(table_path)

top = summary.head(12).iloc[::-1]
fig, (left, right) = plt.subplots(1, 2, figsize=(12, 5), sharey=True)
left.barh(top.index, top["test_mean_abs"], height=0.4, align="edge", label="test split sample")
left.barh(top.index, top["hand_mean_abs"], height=-0.4, align="edge", label="hand labels")
left.set_title("How much each feature moves the score")
left.set_xlabel("mean |SHAP value|")
left.legend()
right.barh(top.index, top["false_pos_mean_push"], color=["#c0392b" if v > 0 else "#7f8c8d" for v in top["false_pos_mean_push"]])
right.axvline(0, color="black", linewidth=0.8)
right.set_title(f"Push on the {int(wrong.sum())} wrong links (red = toward 'match')")
right.set_xlabel("mean SHAP value")
fig.suptitle(f"{args.model_name}: what drives the score")
fig.tight_layout()
fig.savefig(chart_path, dpi=150)

client = mlflow.tracking.MlflowClient()
client.log_artifact(run["run_id"], str(chart_path))
client.log_artifact(run["run_id"], str(table_path))
print(f"\nsaved {chart_path.name} and {table_path.name} to data/reports/ and to the MLflow run")
