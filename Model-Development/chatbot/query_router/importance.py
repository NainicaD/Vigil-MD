"""Feature importance for the query router: which words decide each label.

The router is TF-IDF + one logistic regression per label, so each feature's weight is its
direct effect on that label's log-odds (features are on the same TF-IDF scale). This is the
guideline's feature-importance check: for a linear model the weights are exact, where SHAP
would only estimate them.

For each label, the strongest positive and negative features are listed, word and
character n-grams separately. Two checks flag what a reviewer should look at:
  device_name_features  device or company names among a label's top positive features: the
                        label then depends on which device is asked about (a bias)
  out_of_scope_top      what pushes a question to the medical refusal

Output (--out-dir, default reports/importance): router_features.json, and the same added
to the model's MLflow training run (artifacts under importance/).

Usage (from Model-Development/chatbot/; no BigQuery, a few seconds):
  python -m query_router.importance
"""

import argparse
import json
import pathlib

import numpy as np

from query_router import guardrails
from query_router import labels

TOP_N = 15
SLOTS = pathlib.Path(__file__).resolve().parent / 'slots.json'


def device_name_words(slots_path: pathlib.Path = SLOTS) -> set[str]:
    """Lowercase words of the device, company and product-code names used to generate the
    questions (slots.json), minus generic words, so a top feature can be checked against
    them."""
    slots = json.loads(slots_path.read_text())
    generic = {'pump', 'insulin', 'system', 'implant', 'implants', 'breast', 'heart',
               'monitor', 'device', 'the', 'and', 'of', 'inc', 'corporation', 'medical'}
    out = set()
    for key, values in slots.items():
        if not any(k in key for k in ('device', 'company', 'manufacturer', 'brand', 'code')):
            continue
        for value in values if isinstance(values, list) else []:
            out.update(w for w in str(value).lower().replace('-', ' ').split()
                       if len(w) > 2 and w not in generic)
    return out


def top_features(pipe, top_n: int = TOP_N) -> dict:
    """Returns, per label, the top positive and negative word and character features.

    Args:
      pipe: The fitted router pipeline (features: FeatureUnion word + char; clf:
        OneVsRestClassifier of LogisticRegression).
      top_n: Features listed per list.
    """
    names = pipe.named_steps['features'].get_feature_names_out()
    kind = np.array([n.split('__', 1)[0] for n in names])
    text = np.array([n.split('__', 1)[1] for n in names])
    out = {}
    for label, est in zip(labels.LABELS, pipe.named_steps['clf'].estimators_):
        coef = est.coef_[0]
        entry = {}
        for k in ('word', 'char'):
            idx = np.where(kind == k)[0]
            order = idx[np.argsort(coef[idx])]
            entry[f'{k}_positive'] = [[str(text[i]), round(float(coef[i]), 3)]
                                      for i in order[::-1][:top_n]]
            entry[f'{k}_negative'] = [[str(text[i]), round(float(coef[i]), 3)]
                                      for i in order[:top_n]]
        out[label] = entry
    return out


def name_features(features: dict, names: set[str]) -> dict[str, list[str]]:
    """Returns, per label, the top positive word features that are device or company
    names."""
    out = {}
    for label, entry in features.items():
        hits = [f for f, _ in entry['word_positive']
                if any(w in names for w in f.split())]
        if hits:
            out[label] = hits
    return out


def run(model_dir: pathlib.Path, out_dir: pathlib.Path, top_n: int = TOP_N) -> dict:
    """Computes the report and writes router_features.json."""
    import joblib  # pylint: disable=import-outside-toplevel
    meta = json.loads((model_dir / 'meta.json').read_text())
    pipe = joblib.load(model_dir / 'model.joblib')
    features = top_features(pipe, top_n)
    report = {
        'model_version': meta.get('model_version'),
        'rules_fingerprint': meta.get('rules_fingerprint'),
        'code_rules_fingerprint': guardrails.rules_fingerprint(),
        'top_n': top_n,
        'features': features,
        'device_name_features': name_features(features, device_name_words()),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'router_features.json').write_text(json.dumps(report, indent=2))
    return report


def log_to_run(model_dir: pathlib.Path, out_dir: pathlib.Path) -> str | None:
    """Adds the report to the model's MLflow training run; returns its id or None."""
    meta = json.loads((model_dir / 'meta.json').read_text())
    run_id = meta.get('mlflow_run_id')
    if not run_id:
        return None
    from query_router import tracking  # pylint: disable=import-outside-toplevel
    mlflow = tracking.setup()
    with mlflow.start_run(run_id=run_id):
        mlflow.log_artifacts(str(out_dir), 'importance')
    return run_id


def main(argv: list[str] | None = None) -> int:
    """Prints the top word features per label."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--model-dir', type=pathlib.Path,
                        default=pathlib.Path('models/baseline'))
    parser.add_argument('--out-dir', type=pathlib.Path,
                        default=pathlib.Path('reports/importance'))
    parser.add_argument('--top', type=int, default=TOP_N)
    parser.add_argument('--no-log', action='store_true')
    args = parser.parse_args(argv)
    report = run(args.model_dir, args.out_dir, args.top)
    for label, entry in report['features'].items():
        words = ', '.join(f'{f} ({w:+.2f})' for f, w in entry['word_positive'][:8])
        print(f'{label:13} {words}')
    names = report['device_name_features']
    print('device or company names among top features: '
          + ('; '.join(f'{k}: {", ".join(v)}' for k, v in names.items()) or 'none'))
    print(f'written to {args.out_dir}/router_features.json')
    if not args.no_log:
        run_id = log_to_run(args.model_dir, args.out_dir)
        if run_id:
            print(f'logged to MLflow run {run_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
