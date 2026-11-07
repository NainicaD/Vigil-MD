"""Train the baseline question classifier: TF-IDF (words + characters) + logistic regression,
one classifier per label (multi-label).

Model selection: a small grid over the regularisation strength C, scored by macro-F1 on
the validation questions after per-label threshold tuning (metrics.tune_thresholds).
Validation is scored with the guardrail rules applied, as the chatbot will run it.
Character n-grams make it robust to typos and to device names it has never seen.

Output (--out-dir, default models/baseline):
    model.joblib    the fitted pipeline
    meta.json       labels, thresholds, chosen params, validation metrics, data sizes,
                    the guardrail rules fingerprint, and the MLflow run and model version

MLflow (conventions: Model-Development/MLFLOW.md; store: tracking.py): one parent run per
training session in experiment "chatbot/question-classifier", with one nested run per C.
The parent run holds the required tags, the chosen params, the validation metrics, the
model files, and the model, registered as "chatbot-question-classifier".
evaluate.py later adds the held-out metrics to the same run.

Usage (from Model-Development/chatbot/):
    python -m question_classifier.generate
    python -m question_classifier.train                    # registers a new model version
    python -m question_classifier.train --alias champion   # and points the alias at it
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from . import tracking
from .generate import OUT_DIR, read_jsonl
from .guardrails import rule_hits, rules_fingerprint
from .labels import LABELS, OUT_OF_SCOPE, to_vector
from .metrics import apply_thresholds, score, tune_thresholds

MODEL_DIR = Path("models/baseline")
C_GRID = (0.5, 1.0, 2.0, 4.0, 8.0)


def build_pipeline(C: float):
    from sklearn.linear_model import LogisticRegression
    from sklearn.multiclass import OneVsRestClassifier
    from sklearn.pipeline import FeatureUnion, Pipeline
    from sklearn.feature_extraction.text import TfidfVectorizer
    features = FeatureUnion([
        ("word", TfidfVectorizer(lowercase=True, ngram_range=(1, 2), min_df=2, sublinear_tf=True)),
        ("char", TfidfVectorizer(lowercase=True, analyzer="char_wb", ngram_range=(3, 5),
                                 min_df=2, sublinear_tf=True)),
    ])
    clf = OneVsRestClassifier(LogisticRegression(C=C, max_iter=2000, class_weight="balanced"))
    return Pipeline([("features", features), ("clf", clf)])


def xy(rows: list[dict]) -> tuple[list[str], np.ndarray]:
    return [r["text"] for r in rows], np.array([to_vector(r["labels"]) for r in rows])


def _active_mlflow():
    """mlflow when a run is active (main() opens one), else None: train() can be called
    without tracking, e.g. from tests."""
    import mlflow
    return mlflow if mlflow.active_run() else None


def flat_metrics(metrics: dict, prefix: str) -> dict[str, float]:
    """score() output → MLflow metric names: <prefix>_macro_f1, ..., <prefix>_f1_<label>."""
    out = {f"{prefix}_{k}": float(metrics[k]) for k in
           ("macro_f1", "exact_match", "out_of_scope_recall", "false_refusal_rate")}
    if metrics.get("advice_recall") is not None:
        out[f"{prefix}_advice_recall"] = float(metrics["advice_recall"])
    out.update({f"{prefix}_f1_{lbl}": float(v["f1"]) for lbl, v in metrics["per_label"].items()})
    return out


def train(train_rows: list[dict], val_rows: list[dict], c_grid=C_GRID) -> tuple:
    """Fit one pipeline per C, tune thresholds on validation, keep the best by macro-F1.
    Returns (pipeline, meta). Inside an active MLflow run, each C is a nested run."""
    X_tr, y_tr = xy(train_rows)
    X_va, y_va = xy(val_rows)
    rule_mask = np.array([bool(rule_hits(q)) for q in X_va])
    advice_mask = np.array([r.get("oos_kind") == "advice" for r in val_rows])
    mlflow = _active_mlflow()
    best = None
    for C in c_grid:
        pipe = build_pipeline(C).fit(X_tr, y_tr)
        probs = pipe.predict_proba(X_va)
        thresholds = tune_thresholds(probs, y_va, rule_mask=rule_mask,
                                     advice_mask=advice_mask if advice_mask.any() else None)
        pred = apply_thresholds(probs, thresholds)
        pred[rule_mask, LABELS.index(OUT_OF_SCOPE)] = 1      # score what the chatbot uses
        metrics = score(y_va, pred)
        oos = LABELS.index(OUT_OF_SCOPE)
        metrics["advice_recall"] = (round(float(pred[advice_mask, oos].mean()), 4)
                                    if advice_mask.any() else None)
        if mlflow:
            with mlflow.start_run(run_name=f"tfidf_lr_C{C}", nested=True):
                mlflow.log_params({"model": "tfidf_lr", "C": C})
                mlflow.log_metrics(flat_metrics(metrics, "val"))
        if best is None or metrics["macro_f1"] > best[2]["macro_f1"]:
            best = (pipe, {"C": C}, metrics, thresholds)
    pipe, params, metrics, thresholds = best
    meta = {"model": "tfidf_lr", "labels": list(LABELS), "params": params,
            "rules_fingerprint": rules_fingerprint(),
            "thresholds": thresholds, "val_metrics": metrics,
            "n_train": len(train_rows), "n_val": len(val_rows)}
    return pipe, meta


def save(pipe, meta: dict, out_dir: Path) -> None:
    import joblib
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, out_dir / "model.joblib")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def train_and_log(data_dir: Path, out_dir: Path, c_grid=C_GRID, alias: str | None = None):
    """Train inside one MLflow parent run, register the model, save it. Returns meta."""
    train_path, val_path = data_dir / "train.jsonl", data_dir / "val.jsonl"
    mlflow = tracking.setup()
    with mlflow.start_run(run_name="tfidf_lr-grid") as run:
        mlflow.set_tags({**tracking.required_tags([train_path, val_path]),
                         "rules_fingerprint": rules_fingerprint()})
        pipe, meta = train(read_jsonl(train_path), read_jsonl(val_path), c_grid=c_grid)
        mlflow.log_params({"model": meta["model"], "C": meta["params"]["C"],
                           "c_grid": ",".join(str(c) for c in c_grid),
                           "n_train": meta["n_train"], "n_val": meta["n_val"]})
        mlflow.log_metrics(flat_metrics(meta["val_metrics"], "val"))
        mlflow.log_dict(meta["thresholds"], "thresholds.json")
        info = mlflow.sklearn.log_model(pipe, name="model",
                                        registered_model_name=tracking.REGISTERED_MODEL,
                                        input_example=["How many deaths does the pump have?"])
        version = str(info.registered_model_version)
        meta.update({"mlflow_run_id": run.info.run_id,
                     "registered_model": tracking.REGISTERED_MODEL, "model_version": version})
        save(pipe, meta, out_dir)
        mlflow.log_artifacts(str(out_dir), "files")
    if alias:
        from mlflow import MlflowClient
        MlflowClient().set_registered_model_alias(tracking.REGISTERED_MODEL, alias, version)
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Train the baseline question classifier.")
    ap.add_argument("--data-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--out-dir", type=Path, default=MODEL_DIR)
    ap.add_argument("--alias", default=None,
                    help="point this registry alias (e.g. champion, challenger) at the new version")
    a = ap.parse_args(argv)
    meta = train_and_log(a.data_dir, a.out_dir, alias=a.alias)
    m = meta["val_metrics"]
    print(f"best C={meta['params']['C']}: val macro-F1 {m['macro_f1']}, "
          f"out_of_scope recall {m['out_of_scope_recall']}, "
          f"false refusals {m['false_refusal_rate']}, exact match {m['exact_match']}")
    print("thresholds:", meta["thresholds"])
    print(f"MLflow run {meta['mlflow_run_id']}: {meta['registered_model']} "
          f"version {meta['model_version']}" + (f", alias {a.alias}" if a.alias else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
