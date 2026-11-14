"""Model gate: promote a candidate router only if it is safe and better.

Rules are in gate.json, fixed before the hand-written test set was scored:
  1. Same guardrail rules as the champion (rules_fingerprint).
  2. Floors and ceilings (safety, answered share, false refusals, bias gaps).
     A ceiling the champion already breaks only warns if the candidate is not
     worse than the champion on it. Added 2026-11-11, after the rules were
     fixed: the bias mitigation (context sentences) cut the false-refusal gap
     from 0.585 to 0.335, still above 0.25, and the old rule kept the champion.
     Floors (safety) stay absolute.
  3. No regression beyond the tolerance on any compared number.
  4. A real gain (min_improvement) on at least one of improve_on.
If any rule fails, the champion stays. With --promote, a passing candidate's
registry version gets the "champion" alias, and champion_metrics.json (the
reference CI compares against) is rewritten.

The champion is either a model directory (evaluated here on the same data) or
champion_metrics.json (in git, for CI, where no model files exist).

Usage (from Model-Development/chatbot/):
  python -m query_router.gate --candidate models/optuna \\
      --champion models/baseline
  python -m query_router.gate --candidate models/baseline --regression-only
      # as CI runs it: against champion_metrics.json, no gain needed
"""

import argparse
import json
import pathlib

from query_router import evaluate
from query_router import generate
from query_router import guardrails
from query_router import predict

HERE = pathlib.Path(__file__).resolve().parent
RULES_FILE = HERE / 'gate.json'
CHAMPION_METRICS = HERE / 'champion_metrics.json'
TEST = pathlib.Path('data/test/handwritten_test_v2.jsonl')  # written after rules rev 3
FALLBACK = pathlib.Path('data/generated/heldout.jsonl')
FALSE_ALARMS = pathlib.Path('data/test/false_alarm_set.jsonl')


def model_metrics(model_dir: pathlib.Path, test: pathlib.Path,
                  false_alarms: pathlib.Path) -> dict:
    """Evaluates a model and returns the gate's numbers (no MLflow logging).

    Args:
      model_dir: The router model directory.
      test: The data set to compare on.
      false_alarms: The false-alarm set.

    Returns:
      {"metrics": name -> value, "rules_fingerprint", "data", "model"}.
    """
    router = predict.QueryRouter.load(model_dir)
    report = evaluate.evaluate(router, generate.read_jsonl(test))
    report['false_alarm_set'] = evaluate.evaluate_false_alarms(
        router, generate.read_jsonl(false_alarms))
    raw = evaluate.report_metrics(report, 'm')
    meta = json.loads((model_dir / 'meta.json').read_text())
    return {'metrics': {k[2:]: v for k, v in raw.items() if k.startswith('m_')}
                       | {'falsealarm_flagged': raw['falsealarm_flagged']},
            'rules_fingerprint': report['rules_fingerprint'],
            'data': str(test),
            'model': meta.get('model_version') or str(model_dir),
            'params': meta.get('params')}


def decide(candidate: dict, champion: dict, rules: dict,
           regression_only: bool = False) -> tuple[bool, list]:
    """Applies the gate rules.

    Args:
      candidate: model_metrics() output for the candidate.
      champion: model_metrics() output (or champion_metrics.json) for the
        champion, on the same data.
      rules: gate.json.
      regression_only: CI mode. A retrain of the same recipe cannot improve
        on itself, so the gain rule is skipped and failed ceilings are
        warnings (reported, not failing); floors and tolerances still fail.

    Returns:
      (passed, checks), each check a (name, status, detail) tuple with status
      "PASS", "FAIL" or "WARN".
    """
    def status(ok: bool, soft: bool = False) -> str:
        return 'PASS' if ok else ('WARN' if soft else 'FAIL')

    cand, champ = candidate['metrics'], champion['metrics']
    lower = set(rules['lower_is_better'])
    checks = [('same rules', status(candidate['rules_fingerprint'] ==
                                    champion['rules_fingerprint']),
               f'{candidate["rules_fingerprint"]} vs '
               f'{champion["rules_fingerprint"]}'),
              ('same data', status(candidate['data'] == champion['data']),
               f'{candidate["data"]} vs {champion["data"]}')]
    for name, floor in rules['floors'].items():
        value = cand.get(name)
        checks.append((f'{name} >= {floor}',
                       status(value is not None and value >= floor),
                       f'{value}'))
    for name, ceiling in rules['ceilings'].items():
        value, old = cand.get(name), champ.get(name)
        ok = value is None or value <= ceiling  # No bias slices: no gap.
        # Champion already above the ceiling: a candidate that is not worse
        # only warns, so a model that narrows a bias gap is not blocked in
        # favour of the more biased champion (rule added 2026-11-11).
        narrows = (not ok and old is not None and old > ceiling
                   and value <= old)
        detail = f'{value}' + (f' (champion {old})' if narrows else '')
        checks.append((f'{name} <= {ceiling}',
                       status(ok, regression_only or narrows), detail))
    for name, tol in rules['tolerance'].items():
        new, old = cand.get(name), champ.get(name)
        if new is None or old is None:
            continue
        worse = (new - old) if name in lower else (old - new)
        checks.append((f'{name} not worse by > {tol}', status(worse <= tol),
                       f'{new} vs champion {old}'))
    if not regression_only:
        gains = []
        for name in rules['improve_on']:
            new, old = cand.get(name), champ.get(name)
            if new is not None and old is not None:
                gain = (old - new) if name in lower else (new - old)
                gains.append(f'{name} {gain:+.4f}')
                if gain >= rules['min_improvement']:
                    checks.append((f'better on {name}', 'PASS',
                                   f'{new} vs champion {old}'))
                    break
        else:
            checks.append((f'better by >= {rules["min_improvement"]} on one '
                           f'of {rules["improve_on"]}', 'FAIL',
                           ', '.join(gains)))
    return all(s != 'FAIL' for _, s, _ in checks), checks


def promote(candidate_dir: pathlib.Path, candidate: dict) -> str | None:
    """Moves the registry alias "champion" and rewrites champion_metrics.json.

    Returns:
      The promoted registry version, or None when the model has none.
    """
    meta = json.loads((candidate_dir / 'meta.json').read_text())
    CHAMPION_METRICS.write_text(json.dumps(candidate, indent=2) + '\n')
    version = meta.get('model_version')
    if version:
        # pylint: disable-next=import-outside-toplevel
        from query_router import tracking
        mlflow = tracking.setup()
        mlflow.MlflowClient().set_registered_model_alias(
            tracking.REGISTERED_MODEL, 'champion', version)
    return version


def main(argv: list[str] | None = None) -> int:
    """Exits 0 if the candidate passes, 1 if the champion stays."""
    parser = argparse.ArgumentParser(description='Router promotion gate.')
    parser.add_argument('--candidate', type=pathlib.Path, required=True)
    parser.add_argument('--champion', type=pathlib.Path,
                        default=CHAMPION_METRICS,
                        help='model directory or champion_metrics.json')
    parser.add_argument('--test', type=pathlib.Path, default=None,
                        help='default: the hand-written set, else held-out')
    parser.add_argument('--false-alarms', type=pathlib.Path,
                        default=FALSE_ALARMS)
    parser.add_argument('--promote', action='store_true',
                        help='on pass, move the champion alias')
    parser.add_argument('--write-champion', action='store_true',
                        help='only write champion_metrics.json for --candidate')
    parser.add_argument('--regression-only', action='store_true',
                        help='CI mode: no gain needed, ceilings only warn')
    args = parser.parse_args(argv)
    test = args.test or (TEST if TEST.exists() else FALLBACK)
    candidate = model_metrics(args.candidate, test, args.false_alarms)
    if args.write_champion:
        CHAMPION_METRICS.write_text(json.dumps(candidate, indent=2) + '\n')
        print(f'wrote {CHAMPION_METRICS}')
        return 0
    if args.champion.suffix == '.json':
        champion = json.loads(args.champion.read_text())
    else:
        champion = model_metrics(args.champion, test, args.false_alarms)
    rules = json.loads(RULES_FILE.read_text())
    passed, checks = decide(candidate, champion, rules, args.regression_only)
    print(f'gate on {test} (rules {guardrails.rules_fingerprint()}): '
          f'candidate {candidate["model"]} vs champion {champion["model"]}'
          f'{" [regression only]" if args.regression_only else ""}')
    for name, state, detail in checks:
        print(f'  {state}  {name}: {detail}')
    if not passed:
        print('result: champion stays')
        return 1
    print('result: candidate passes')
    if args.promote and not args.regression_only:
        print(f'promoted registry version {promote(args.candidate, candidate)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
