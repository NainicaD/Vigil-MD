"""Train the baseline question classifier: TF-IDF (words + characters) + logistic regression,
one classifier per label (multi-label).

Model selection: a small grid over the regularisation strength C, scored by macro-F1 on
the validation questions after per-label threshold tuning (metrics.tune_thresholds).
Validation is scored with the guardrail rules applied, as the chatbot will run it.
Character n-grams make it robust to typos and to device names it has never seen.

Output (--out-dir, default models/baseline):
    model.joblib    the fitted pipeline
    meta.json       labels, thresholds, chosen params, validation metrics, data sizes
If MLflow is installed, every grid point is also logged as a run (experiment
"question-classifier").

Usage (from Model-Pipeline/):
    python -m question_classifier.generate
    python -m question_classifier.train
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .generate import OUT_DIR, read_jsonl
from .guardrails import rule_hits
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


def _mlflow():
    try:
        import mlflow
        return mlflow
    except ImportError:
        return None


def train(train_rows: list[dict], val_rows: list[dict], c_grid=C_GRID) -> tuple:
    """Fit one pipeline per C, tune thresholds on validation, keep the best by macro-F1.
    Returns (pipeline, meta)."""
    X_tr, y_tr = xy(train_rows)
    X_va, y_va = xy(val_rows)
    rule_mask = np.array([bool(rule_hits(q)) for q in X_va])
    mlflow = _mlflow()
    if mlflow:
        mlflow.set_experiment("question-classifier")
    best = None
    for C in c_grid:
        pipe = build_pipeline(C).fit(X_tr, y_tr)
        probs = pipe.predict_proba(X_va)
        thresholds = tune_thresholds(probs, y_va, rule_mask=rule_mask)
        pred = apply_thresholds(probs, thresholds)
        pred[rule_mask, LABELS.index(OUT_OF_SCOPE)] = 1      # score what the chatbot uses
        metrics = score(y_va, pred)
        if mlflow:
            with mlflow.start_run(run_name=f"tfidf_lr_C{C}"):
                mlflow.log_params({"model": "tfidf_lr", "C": C})
                mlflow.log_metrics({"val_macro_f1": metrics["macro_f1"],
                                    "val_oos_recall": metrics["out_of_scope_recall"],
                                    "val_false_refusal_rate": metrics["false_refusal_rate"]})
        if best is None or metrics["macro_f1"] > best[2]["macro_f1"]:
            best = (pipe, {"C": C}, metrics, thresholds)
    pipe, params, metrics, thresholds = best
    meta = {"model": "tfidf_lr", "labels": list(LABELS), "params": params,
            "thresholds": thresholds, "val_metrics": metrics,
            "n_train": len(train_rows), "n_val": len(val_rows)}
    return pipe, meta


def save(pipe, meta: dict, out_dir: Path) -> None:
    import joblib
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, out_dir / "model.joblib")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Train the baseline question classifier.")
    ap.add_argument("--data-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--out-dir", type=Path, default=MODEL_DIR)
    a = ap.parse_args(argv)
    pipe, meta = train(read_jsonl(a.data_dir / "train.jsonl"), read_jsonl(a.data_dir / "val.jsonl"))
    save(pipe, meta, a.out_dir)
    m = meta["val_metrics"]
    print(f"best C={meta['params']['C']}: val macro-F1 {m['macro_f1']}, "
          f"out_of_scope recall {m['out_of_scope_recall']}, "
          f"false refusals {m['false_refusal_rate']}, exact match {m['exact_match']}")
    print("thresholds:", meta["thresholds"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
