"""Optuna search over the router's TF-IDF + logistic regression settings.

Each trial fits one setting and scores it on validation exactly as train.py does
(thresholds tuned per label, rule backstop applied). Objective: validation
macro-F1, but a trial whose advice recall is below the safety target scores
lower than any trial that meets it, so the search never trades safety for F1.

The best setting is retrained, saved to models/optuna and registered in MLflow
as a new version of chatbot-query-router with the alias "challenger". It never
replaces "champion" here: gate.py compares the two on the test set first.

Usage (from Model-Development/chatbot/):
  python -m query_router.tune --trials 30
"""

import argparse
import pathlib

import numpy as np

from query_router import generate
from query_router import guardrails
from query_router import metrics as router_metrics
from query_router import tracking
from query_router import train

OUT_DIR = pathlib.Path('models/optuna')
ALIAS = 'challenger'


def suggest(trial) -> dict:
    """Returns one setting to try (keys are train.build_pipeline arguments)."""
    char_min = trial.suggest_int('char_ngram_min', 2, 3)
    return {
        'C': trial.suggest_float('C', 0.1, 20.0, log=True),
        'word_ngram_max': trial.suggest_int('word_ngram_max', 1, 3),
        'char_ngram_min': char_min,
        'char_ngram_max': trial.suggest_int('char_ngram_max', char_min + 2, 6),
        'min_df': trial.suggest_int('min_df', 1, 3),
        'class_weight': trial.suggest_categorical('class_weight',
                                                  ['balanced', None]),
    }


def objective_value(metrics: dict,
                    target: float = router_metrics.OOS_RECALL_TARGET) -> float:
    """Returns macro-F1, minus 1 when advice recall misses the target.

    Args:
      metrics: train.fit_and_score() metrics (macro_f1, advice_recall).
      target: The advice recall a trial must reach on validation.

    Returns:
      A value where every safe trial beats every unsafe one.
    """
    recall = metrics.get('advice_recall')
    safe = recall is None or recall >= target
    return metrics['macro_f1'] - (0.0 if safe else 1.0)


def tune(train_rows: list[dict], val_rows: list[dict], n_trials: int,
         seed: int = 13, mlflow=None) -> tuple:
    """Runs the search.

    Args:
      train_rows: Training questions.
      val_rows: Validation questions.
      n_trials: Number of settings to try.
      seed: Sampler seed, so a search can be repeated.
      mlflow: The mlflow module inside an active run (each trial is logged as
        a nested run), or None.

    Returns:
      (best params, best metrics, best thresholds, optuna study).
    """
    import optuna  # pylint: disable=import-outside-toplevel
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    results = {}

    def objective(trial):
        params = suggest(trial)
        _, scores, thresholds = train.fit_and_score(params, train_rows,
                                                    val_rows)
        results[trial.number] = (params, scores, thresholds)
        if mlflow:
            with mlflow.start_run(run_name=f'tfidf_lr_trial{trial.number}',
                                  nested=True):
                mlflow.log_params({'model': 'tfidf_lr', **params})
                mlflow.log_metrics(train.flat_metrics(scores, 'val'))
        return objective_value(scores)

    study = optuna.create_study(
        direction='maximize', sampler=optuna.samplers.TPESampler(seed=seed))
    study.enqueue_trial({'C': 1.0, 'word_ngram_max': 2, 'char_ngram_min': 3,
                         'char_ngram_max': 5, 'min_df': 2,
                         'class_weight': 'balanced'})  # The baseline first.
    study.optimize(objective, n_trials=n_trials)
    params, scores, thresholds = results[study.best_trial.number]
    return params, scores, thresholds, study


def main(argv: list[str] | None = None) -> int:
    """Searches, retrains the best setting, registers it as challenger."""
    parser = argparse.ArgumentParser(
        description='Optuna search for the router.')
    parser.add_argument('--data-dir', type=pathlib.Path,
                        default=generate.OUT_DIR)
    parser.add_argument('--out-dir', type=pathlib.Path, default=OUT_DIR)
    parser.add_argument('--trials', type=int, default=30)
    parser.add_argument('--seed', type=int, default=13)
    args = parser.parse_args(argv)
    train_path = args.data_dir / 'train.jsonl'
    val_path = args.data_dir / 'val.jsonl'
    train_rows = generate.read_jsonl(train_path)
    val_rows = generate.read_jsonl(val_path)

    mlflow = tracking.setup()
    with mlflow.start_run(run_name='tfidf_lr-optuna') as run:
        mlflow.set_tags({**tracking.required_tags([train_path, val_path]),
                         'rules_fingerprint': guardrails.rules_fingerprint()})
        params, scores, _, study = tune(train_rows, val_rows, args.trials,
                                        args.seed, mlflow)
        pipe, scores, thresholds = train.fit_and_score(params, train_rows,
                                                       val_rows)
        mlflow.log_params({'model': 'tfidf_lr', 'search': 'optuna-tpe',
                           'trials': args.trials, 'seed': args.seed, **params,
                           'n_train': len(train_rows), 'n_val': len(val_rows)})
        mlflow.log_metrics(train.flat_metrics(scores, 'val'))
        mlflow.log_dict(thresholds, 'thresholds.json')
        mlflow.log_dict({str(t.number): {'value': t.value, **t.params}
                         for t in study.trials}, 'optuna_trials.json')
        # pylint: disable-next=import-outside-toplevel
        from mlflow.models import infer_signature
        example = np.array(['How many deaths does the pump have?'])
        info = mlflow.sklearn.log_model(
            pipe, name='model', registered_model_name=tracking.REGISTERED_MODEL,
            signature=infer_signature(example, pipe.predict_proba(example)),
            input_example=example)
        version = str(info.registered_model_version)
        meta = {'model': 'tfidf_lr', 'labels': list(train.LABELS),
                'params': params,
                'rules_fingerprint': guardrails.rules_fingerprint(),
                'thresholds': thresholds, 'val_metrics': scores,
                'n_train': len(train_rows), 'n_val': len(val_rows),
                'search': {'method': 'optuna-tpe', 'trials': args.trials,
                           'seed': args.seed},
                'mlflow_run_id': run.info.run_id,
                'registered_model': tracking.REGISTERED_MODEL,
                'model_version': version}
        train.save(pipe, meta, args.out_dir)
        mlflow.log_artifacts(str(args.out_dir), 'files')
    mlflow.MlflowClient().set_registered_model_alias(
        tracking.REGISTERED_MODEL, ALIAS, version)
    print(f'best of {args.trials}: {params}')
    print(f'val macro-F1 {scores["macro_f1"]}, advice recall '
          f'{scores["advice_recall"]}, false refusals '
          f'{scores["false_refusal_rate"]}')
    print(f'MLflow run {run.info.run_id}: {tracking.REGISTERED_MODEL} '
          f'version {version}, alias {ALIAS}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
