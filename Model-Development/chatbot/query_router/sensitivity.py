"""Sensitivity of the router's safety numbers to the out_of_scope threshold.

For each threshold on a grid, with the rule backstop applied (what the chatbot
runs), per data set:
  advice_recall       share of medical-advice questions flagged out_of_scope
  false_refusal_rate  share of in-scope questions flagged out_of_scope
It shows how much safety the chosen threshold buys, what it costs in false
refusals, and how steep the trade-off is around it.

Output: a CSV (one row per threshold and data set), a JSON summary, and a PNG
when matplotlib is installed; all are also logged to the model's MLflow run.

Usage (from Model-Development/chatbot/):
  python -m query_router.sensitivity --model-dir models/baseline
"""

import argparse
import csv
import json
import pathlib

import numpy as np

from query_router import generate
from query_router import guardrails
from query_router import labels
from query_router import predict

GRID = tuple(round(t, 2) for t in np.arange(0.05, 0.96, 0.05))
DATA_SETS = {
    'val': 'data/generated/val.jsonl',
    'heldout': 'data/generated/heldout.jsonl',
    'test': 'data/test/handwritten_test.jsonl',
    'false_alarm': 'data/test/false_alarm_set.jsonl',
}
OOS = labels.LABELS.index(labels.OUT_OF_SCOPE)


def sweep(oos_scores: np.ndarray, rule_mask: np.ndarray, rows: list[dict],
          grid=GRID) -> list[dict]:
    """Returns the safety numbers at each threshold.

    Args:
      oos_scores: The model's out_of_scope probability per question.
      rule_mask: True where a guardrail rule fires.
      rows: The questions (labels, and oos_kind on out_of_scope rows).
      grid: Thresholds to try.

    Returns:
      One dict per threshold: threshold, advice_recall, false_refusal_rate
      (None when the data set has no such questions), n_advice, n_in_scope.
    """
    advice = np.array([r.get('oos_kind') == 'advice' for r in rows])
    in_scope = np.array([labels.OUT_OF_SCOPE not in r['labels'] for r in rows])
    out = []
    for t in grid:
        flagged = (oos_scores >= t) | rule_mask
        out.append({
            'threshold': float(t),
            'advice_recall': (round(float(flagged[advice].mean()), 4)
                              if advice.any() else None),
            'false_refusal_rate': (round(float(flagged[in_scope].mean()), 4)
                                   if in_scope.any() else None),
            'n_advice': int(advice.sum()), 'n_in_scope': int(in_scope.sum()),
        })
    return out


def summarize(curve: list[dict], chosen: float, target: float) -> dict:
    """Returns the numbers at the chosen threshold and the safe range.

    Args:
      curve: sweep() output for one data set.
      chosen: The model's out_of_scope threshold.
      target: The advice recall the threshold must keep.

    Returns:
      at_chosen (the nearest grid point), max_safe_threshold (highest
      threshold still at the target, or None), and the false refusal rate
      at that threshold.
    """
    at = min(curve, key=lambda p: abs(p['threshold'] - chosen))
    safe = [p for p in curve if p['advice_recall'] is not None
            and p['advice_recall'] >= target]
    best = max(safe, key=lambda p: p['threshold']) if safe else None
    return {'at_chosen': at,
            'max_safe_threshold': best['threshold'] if best else None,
            'false_refusal_at_max_safe': (best['false_refusal_rate']
                                          if best else None)}


def plot(curves: dict[str, list[dict]], chosen: float,
         path: pathlib.Path) -> bool:
    """Draws advice recall and false refusals against the threshold.

    Returns:
      False when matplotlib is not installed (no file is written).
    """
    try:
        import matplotlib  # pylint: disable=import-outside-toplevel
        matplotlib.use('Agg')
        from matplotlib import pyplot  # pylint: disable=import-outside-toplevel
    except ImportError:
        return False
    fig, axes = pyplot.subplots(1, 2, figsize=(11, 4), sharex=True)
    for name, curve in curves.items():
        xs = [p['threshold'] for p in curve]
        for ax, key in zip(axes, ('advice_recall', 'false_refusal_rate')):
            ys = [p[key] for p in curve]
            if any(y is not None for y in ys):
                ax.plot(xs, ys, marker='o', markersize=3, label=name)
    for ax, title in zip(axes, ('Medical-advice questions flagged',
                                'Normal questions flagged by mistake')):
        ax.axvline(chosen, color='grey', linestyle='--', linewidth=1)
        ax.set_title(title)
        ax.set_xlabel('out_of_scope threshold (rules always on)')
        ax.set_ylim(0, 1.02)
        ax.grid(alpha=0.3)
    axes[1].legend()  # The right panel has every data set.
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    pyplot.close(fig)
    return True


def run(model_dir: pathlib.Path, out_dir: pathlib.Path,
        data_sets: dict[str, str] | None = None) -> dict:
    """Sweeps every data set that exists and writes the outputs.

    Args:
      model_dir: The router model directory.
      out_dir: Where the CSV, JSON and PNG go.
      data_sets: Name -> JSONL path; defaults to DATA_SETS.

    Returns:
      The JSON summary.
    """
    from query_router import metrics  # pylint: disable=import-outside-toplevel
    router = predict.QueryRouter.load(model_dir)
    chosen = router.thresholds[labels.OUT_OF_SCOPE]
    curves = {}
    for name, path in (data_sets or DATA_SETS).items():
        if not pathlib.Path(path).exists():
            continue
        rows = generate.read_jsonl(pathlib.Path(path))
        texts = [r['text'] for r in rows]
        probs = router.pipe.predict_proba(texts)[:, OOS]
        rules = np.array([bool(guardrails.rule_hits(q)) for q in texts])
        curves[name] = sweep(probs, rules, rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / 'oos_threshold.csv', 'w', newline='',
              encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['data_set', 'threshold',
                                               'advice_recall',
                                               'false_refusal_rate',
                                               'n_advice', 'n_in_scope'])
        writer.writeheader()
        for name, curve in curves.items():
            for point in curve:
                writer.writerow({'data_set': name, **point})
    summary = {'rules_fingerprint': guardrails.rules_fingerprint(),
               'chosen_threshold': chosen,
               'target_advice_recall': metrics.OOS_RECALL_TARGET,
               'data_sets': {n: summarize(c, chosen, metrics.OOS_RECALL_TARGET)
                             for n, c in curves.items()}}
    (out_dir / 'oos_threshold.json').write_text(json.dumps(summary, indent=2))
    summary['plot'] = plot(curves, chosen, out_dir / 'oos_threshold.png')
    return summary


def log_to_run(model_dir: pathlib.Path, out_dir: pathlib.Path) -> str | None:
    """Adds the outputs to the model's MLflow run; returns its id or None."""
    meta = json.loads((model_dir / 'meta.json').read_text())
    run_id = meta.get('mlflow_run_id')
    if not run_id:
        return None
    from query_router import tracking  # pylint: disable=import-outside-toplevel
    mlflow = tracking.setup()
    with mlflow.start_run(run_id=run_id):
        mlflow.log_artifacts(str(out_dir), 'sensitivity')
    return run_id


def main(argv: list[str] | None = None) -> int:
    """Prints the safety numbers around the chosen threshold."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--model-dir', type=pathlib.Path,
                        default=pathlib.Path('models/baseline'))
    parser.add_argument('--out-dir', type=pathlib.Path,
                        default=pathlib.Path('reports/sensitivity'))
    parser.add_argument('--no-log', action='store_true')
    args = parser.parse_args(argv)
    summary = run(args.model_dir, args.out_dir)
    print(f'chosen out_of_scope threshold {summary["chosen_threshold"]}, '
          f'rules {summary["rules_fingerprint"]}')
    for name, s in summary['data_sets'].items():
        at = s['at_chosen']
        print(f'  {name:12} at {at["threshold"]}: advice recall '
              f'{at["advice_recall"]}, false refusals '
              f'{at["false_refusal_rate"]}; highest threshold keeping '
              f'{summary["target_advice_recall"]}: '
              f'{s["max_safe_threshold"]} (false refusals '
              f'{s["false_refusal_at_max_safe"]})')
    print(f'written to {args.out_dir}/ (plot: {summary["plot"]})')
    if not args.no_log:
        run_id = log_to_run(args.model_dir, args.out_dir)
        if run_id:
            print(f'logged to MLflow run {run_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
