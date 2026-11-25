"""Compare model types for the router on the same features, chosen on validation only.

Each type gets a small grid; every setting is fitted on train and scored on val exactly as
train.py does (thresholds tuned per label, the rule backstop applied). The best setting of
each type is chosen with tune.objective_value (safety first: a setting under the advice-recall
target on validation loses to every setting above it), saved to models/<type>/, registered in
MLflow, and listed. The test sets are not read here: gate.py scores the candidates once.

  lr   logistic regression (the baseline's model), C = regularisation
  svm  linear SVM with calibrated probabilities, C = regularisation
  nb   complement naive Bayes, C = smoothing alpha
  minilm         frozen MiniLM sentence embeddings + logistic regression (item C, embed.py)
  minilm_tfidf   the same, with the TF-IDF features added
  distilbert     DistilBERT fine-tuned end to end (item C, finetune.py); grid over
                 learning rate and epochs instead of C

Usage (from Model-Development/chatbot/; local, about a minute):
  python -m query_router.compare
  python -m query_router.compare --types minilm,minilm_tfidf   # needs requirements-tf.txt
  python -m query_router.compare --types distilbert             # the same; about 5 min per setting
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
GRIDS = {"lr": (0.5, 1.0, 2.0, 4.0), "svm": (0.1, 0.5, 1.0, 2.0, 4.0), "nb": (0.1, 0.3, 1.0, 3.0, 10.0),
         # Unit-length embeddings: weaker features per dimension, so larger C values.
         "minilm": (1.0, 4.0, 16.0, 64.0), "minilm_tfidf": (1.0, 4.0, 16.0),
         # The usual fine-tuning range for BERT-sized models; a setting is a dict, not a C.
         "distilbert": ({"lr": 3e-5, "epochs": 3}, {"lr": 5e-5, "epochs": 3}, {"lr": 5e-5, "epochs": 5})}
DEFAULT_TYPES = ("lr", "svm", "nb")
MODELS_DIR = pathlib.Path("models")


def best_per_type(train_rows: list[dict], val_rows: list[dict], grids: dict | None = None,
                  mlflow=None) -> dict:
    """Returns {type: (params, pipeline, val metrics, thresholds)}, the best of each grid.
    grids defaults to the TF-IDF types (DEFAULT_TYPES)."""
    grids = grids if grids is not None else {k: GRIDS[k] for k in DEFAULT_TYPES}
    out = {}
    for kind, grid in grids.items():
        for c in grid:
            params = {"model": kind, **c} if isinstance(c, dict) else {"model": kind, "C": c}
            pipe, scores, thresholds = train.fit_and_score(params, train_rows, val_rows)
            if mlflow:
                name = "_".join(f"{k}{v}" for k, v in c.items()) if isinstance(c, dict) else f"C{c}"
                with mlflow.start_run(run_name=f"{kind}_{name}", nested=True):
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
    if kind in train.FINETUNED_TYPES:
        return _save_finetuned_candidate(kind, params, pipe, scores, thresholds, n_train, n_val, mlflow)
    embedding = kind in train.EMBEDDING_TYPES
    meta = {"model": kind if embedding else f"tfidf_{kind}", "labels": list(labels.LABELS),
            "params": params, "rules_fingerprint": guardrails.rules_fingerprint(),
            "thresholds": thresholds, "val_metrics": scores, "n_train": n_train, "n_val": n_val}
    out_dir = MODELS_DIR / kind
    if embedding:
        from query_router.embed import DEFAULT_MODEL  # pylint: disable=import-outside-toplevel
        meta["encoder"] = DEFAULT_MODEL
        _save_embedding_candidate(pipe, meta, out_dir, scores, params, mlflow)
        return meta
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


def _save_embedding_candidate(pipe, meta: dict, out_dir: pathlib.Path, scores: dict,
                              params: dict, mlflow=None) -> None:
    """model.joblib (settings and the regression) + encoder/ (the MiniLM weights). Logged to
    MLflow as files, not registered: the registry's safe format cannot hold the encoder step,
    and a candidate is only registered once it passes the gate."""
    from query_router.embed import SentenceEmbedder  # pylint: disable=import-outside-toplevel
    from query_router.predict import _steps  # pylint: disable=import-outside-toplevel
    train.save(pipe, meta, out_dir)
    next(s for s in _steps(pipe) if isinstance(s, SentenceEmbedder)).save_encoder(out_dir)
    if mlflow:
        with mlflow.start_run(run_name=f"{meta['model']}-best", nested=True) as run:
            mlflow.log_params({**params, "selected_on": "val", "encoder": meta["encoder"]})
            mlflow.log_metrics(train.flat_metrics(scores, "val"))
            meta["mlflow_run_id"] = run.info.run_id
            train.save(pipe, meta, out_dir)
            mlflow.log_artifacts(str(out_dir), "files")


def _save_finetuned_candidate(kind: str, params: dict, pipe, scores: dict, thresholds: dict,
                              n_train: int, n_val: int, mlflow=None) -> dict:
    """model.joblib (the settings) + finetuned/ (the network and tokenizer). Logged to MLflow as
    files, not registered, like the MiniLM candidates: a candidate is registered only once it
    passes the gate."""
    from query_router.finetune import DEFAULT_MODEL  # pylint: disable=import-outside-toplevel
    meta = {"model": kind, "labels": list(labels.LABELS), "params": params,
            "rules_fingerprint": guardrails.rules_fingerprint(), "thresholds": thresholds,
            "val_metrics": scores, "n_train": n_train, "n_val": n_val,
            "finetuned": DEFAULT_MODEL}
    out_dir = MODELS_DIR / kind
    train.save(pipe, meta, out_dir)
    pipe.named_steps["clf"].save_weights(out_dir)
    if mlflow:
        with mlflow.start_run(run_name=f"{kind}-best", nested=True) as run:
            mlflow.log_params({**params, "selected_on": "val", "base_model": DEFAULT_MODEL})
            mlflow.log_metrics(train.flat_metrics(scores, "val"))
            meta["mlflow_run_id"] = run.info.run_id
            train.save(pipe, meta, out_dir)
            mlflow.log_artifact(str(out_dir / "meta.json"), "files")
    return meta


def main(argv: list[str] | None = None) -> int:
    """Prints the validation results per model type."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=pathlib.Path, default=generate.OUT_DIR)
    parser.add_argument("--no-log", action="store_true")
    parser.add_argument("--types", default=",".join(DEFAULT_TYPES),
                        help=f"comma-separated, from {', '.join(GRIDS)}")
    args = parser.parse_args(argv)
    train_path, val_path = args.data_dir / "train.jsonl", args.data_dir / "val.jsonl"
    train_rows, val_rows = generate.read_jsonl(train_path), generate.read_jsonl(val_path)
    mlflow = None if args.no_log else tracking.setup()
    summary = {}

    def run():
        best = best_per_type(train_rows, val_rows, mlflow=mlflow,
                             grids={k: GRIDS[k] for k in args.types.split(",")})
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
        setting = s["params"].get("C", ",".join(f"{k}={v}" for k, v in s["params"].items() if k != "model"))
        print(f"{kind:5} {setting!s:>8} {s['macro_f1']:>9} {s['exact_match']:>7} "
              f"{s['false_refusal_rate']:>11} {s['advice_recall']:>12} {str(s['model_version']):>8}")
    pathlib.Path("reports/compare").mkdir(parents=True, exist_ok=True)
    name = "comparison.json" if args.types == ",".join(DEFAULT_TYPES) else \
        f"comparison_{args.types.replace(',', '_')}.json"
    pathlib.Path("reports/compare", name).write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
