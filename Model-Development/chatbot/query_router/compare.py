"""Compare model types for the router on the same features, chosen on validation only.

Each type gets a small grid; every setting is fitted on train and scored on val exactly as
train.py does (thresholds tuned per label, the rule backstop applied). The best setting of
each type is chosen with tune.objective_value (safety first: a setting under the advice-recall
target on validation loses to every setting above it), saved to models/<type>/, registered in
MLflow, and listed. The test sets are not read here: gate.py scores the candidates once.

  lr   logistic regression (the baseline's model), C = regularisation
  svm  linear SVM with calibrated probabilities, C = regularisation
  nb   complement naive Bayes, C = smoothing alpha

Usage (from Model-Development/chatbot/; local, about a minute):
  python -m query_router.compare
"""
import argparse
import json
import pathlib

import numpy as np

from query_router import generate
from query_router import guardrails
from query_router import labels
from query_router import tracking
from query_router import train
from query_router import tune

# scikit-learn's own calibration classes inside the SVM: MLflow's safe (skops) format asks for
# them to be named before it saves the model
TRUSTED = {"svm": ["sklearn.calibration._CalibratedClassifier",
                   "sklearn.calibration._SigmoidCalibration"]}
GRIDS = {"lr": (0.5, 1.0, 2.0, 4.0), "svm": (0.1, 0.5, 1.0, 2.0, 4.0), "nb": (0.1, 0.3, 1.0, 3.0, 10.0)}
MODELS_DIR = pathlib.Path("models")


def best_per_type(train_rows: list[dict], val_rows: list[dict], grids: dict = GRIDS,
                  mlflow=None) -> dict:
    """Returns {type: (params, pipeline, val metrics, thresholds)}, the best of each grid."""
    out = {}
    for kind, grid in grids.items():
        for c in grid:
            params = {"model": kind, "C": c}
            pipe, scores, thresholds = train.fit_and_score(params, train_rows, val_rows)
            if mlflow:
                with mlflow.start_run(run_name=f"{kind}_C{c}", nested=True):
                    mlflow.log_params(params)
                    mlflow.log_metrics(train.flat_metrics(scores, "val"))
            value = tune.objective_value(scores)
            if kind not in out or value > tune.objective_value(out[kind][2]):
                out[kind] = (params, pipe, scores, thresholds)
    return out


def save_candidate(kind: str, params: dict, pipe, scores: dict, thresholds: dict,
                   n_train: int, n_val: int, mlflow=None) -> dict:
    """Saves one candidate like train.py does (meta.json + model.joblib), registered when an
    MLflow run is active. Returns its meta."""
    meta = {"model": f"tfidf_{kind}", "labels": list(labels.LABELS), "params": params,
            "rules_fingerprint": guardrails.rules_fingerprint(), "thresholds": thresholds,
            "val_metrics": scores, "n_train": n_train, "n_val": n_val}
    out_dir = MODELS_DIR / kind
    if mlflow:
        from mlflow.models import infer_signature  # pylint: disable=import-outside-toplevel
        with mlflow.start_run(run_name=f"{kind}-best", nested=True) as run:
            mlflow.log_params({**params, "selected_on": "val"})
            mlflow.log_metrics(train.flat_metrics(scores, "val"))
            example = np.array(["How many deaths does the pump have?"])
            info = mlflow.sklearn.log_model(
                pipe, name="model", registered_model_name=tracking.REGISTERED_MODEL,
                signature=infer_signature(example, pipe.predict_proba(example)),
                input_example=example,
                **({"skops_trusted_types": TRUSTED[kind]} if kind in TRUSTED else {}))
            meta.update({"mlflow_run_id": run.info.run_id,
                         "registered_model": tracking.REGISTERED_MODEL,
                         "model_version": str(info.registered_model_version)})
            train.save(pipe, meta, out_dir)
            mlflow.log_artifacts(str(out_dir), "files")
    else:
        train.save(pipe, meta, out_dir)
    return meta


def main(argv: list[str] | None = None) -> int:
    """Prints the validation results per model type."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=pathlib.Path, default=generate.OUT_DIR)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args(argv)
    train_path, val_path = args.data_dir / "train.jsonl", args.data_dir / "val.jsonl"
    train_rows, val_rows = generate.read_jsonl(train_path), generate.read_jsonl(val_path)
    mlflow = None if args.no_log else tracking.setup()
    summary = {}

    def run():
        best = best_per_type(train_rows, val_rows, mlflow=mlflow)
        for kind, (params, pipe, scores, thresholds) in best.items():
            meta = save_candidate(kind, params, pipe, scores, thresholds, len(train_rows),
                                  len(val_rows), mlflow=mlflow)
            summary[kind] = {"params": params, "model_version": meta.get("model_version"),
                             **{k: scores[k] for k in ("macro_f1", "exact_match",
                                                        "false_refusal_rate", "advice_recall")}}

    if mlflow:
        with mlflow.start_run(run_name="model-type-comparison"):
            mlflow.set_tags({**tracking.required_tags([train_path, val_path]),
                             "rules_fingerprint": guardrails.rules_fingerprint()})
            run()
            mlflow.log_dict(summary, "comparison.json")
    else:
        run()
    print(f"validation (rules {guardrails.rules_fingerprint()}); test sets not read")
    print(f"{'type':5} {'C/alpha':>8} {'macro-F1':>9} {'exact':>7} {'false ref.':>11} "
          f"{'advice rec.':>12} {'version':>8}")
    for kind, s in summary.items():
        print(f"{kind:5} {s['params']['C']:>8} {s['macro_f1']:>9} {s['exact_match']:>7} "
              f"{s['false_refusal_rate']:>11} {s['advice_recall']:>12} {str(s['model_version']):>8}")
    pathlib.Path("reports/compare").mkdir(parents=True, exist_ok=True)
    pathlib.Path("reports/compare/comparison.json").write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
