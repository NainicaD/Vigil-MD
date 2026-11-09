"""End-to-end evaluation of chatbot v1 against reference answers.

Each row of data/test/e2e_dev.jsonl (written independently of the rules and
templates) gives a question, its labels, the expected status, the device the
answer must name, and whether it must start with the medical refusal. Every
question runs through the whole chatbot on BigQuery (vigil_md_dev by default)
and is checked for:
  status    the right kind of reply (answered, refused, help, not_found, ...)
  entity    an answered question names the expected device, code or company
  refusal   the medical refusal comes first exactly when advice was asked
  sections  every factual label asked for has its section (answered rows)
A row is correct when status, entity and refusal are all right.

Results go to MLflow (experiment chatbot/answer-v1) with the router version,
the rules and template fingerprints and the search index used, so template
and router changes can be compared on the same questions.

Usage (from Model-Development/chatbot/; costs a few tens of MB on dev):
  python -m answer.evaluate                       # vigil_md_dev
  python -m answer.evaluate --no-log --out reports/e2e_report.json
"""

import argparse
import collections
import json
import pathlib
import time

from answer import pipeline
from answer import templates
from query_router import generate
from query_router import guardrails
from query_router import labels
from query_router import tracking

E2E = pathlib.Path('data/test/e2e_dev.jsonl')
EXPERIMENT = 'chatbot/answer-v1'
FACTUAL = tuple(n for n in labels.LABELS if n != labels.OUT_OF_SCOPE)


def check(row: dict, out: dict) -> dict:
    """Compares one answer with its reference.

    Args:
      row: The reference (text, labels, expected_status, expected_entity,
        refusal_expected).
      out: Chatbot.answer() output.

    Returns:
      The four checks (sections None when not applicable) and correct.
    """
    status_ok = out['status'] == row['expected_status']
    entity_ok = True
    if row.get('expected_entity') and row['expected_status'] == 'answered':
        want = row['expected_entity'].lower()
        names = [str(e.get(k) or '').lower() for e in out['entities']
                 for k in ('title', 'id', 'subtitle')]
        entity_ok = any(want in n for n in names)
    refused = (out['reply_kind'] == 'medical_refusal'
               and out['text'].startswith(guardrails.REFUSAL))
    refusal_ok = refused == bool(row.get('refusal_expected'))
    sections = None
    asked = [n for n in row['labels'] if n in FACTUAL]
    if row['expected_status'] == 'answered' and asked and status_ok:
        shown = {s['kind'] for s in out['sections']}
        sections = round(sum(n in shown for n in asked) / len(asked), 4)
    return {'status': status_ok, 'entity': entity_ok, 'refusal': refusal_ok,
            'sections': sections,
            'correct': status_ok and entity_ok and refusal_ok}


def summarize(results: list[dict]) -> dict:
    """Returns accuracy overall, per check, per expected status and per label.

    Args:
      results: One dict per question: row, checks, cost and seconds.
    """
    def rate(items, key='correct'):
        vals = [r['checks'][key] for r in items
                if r['checks'][key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    by_status = collections.defaultdict(list)
    by_label = collections.defaultdict(list)
    for r in results:
        by_status[r['row']['expected_status']].append(r)
        for name in r['row']['labels'] or ['(none)']:
            by_label[name].append(r)
    costs = [r['billed_gb'] for r in results]
    return {
        'n': len(results),
        'accuracy': rate(results),
        'checks': {k: rate(results, k)
                   for k in ('status', 'entity', 'refusal', 'sections')},
        'by_expected_status': {k: {'n': len(v), 'accuracy': rate(v)}
                               for k, v in sorted(by_status.items())},
        'by_label': {k: {'n': len(v), 'accuracy': rate(v)}
                     for k, v in sorted(by_label.items())},
        'billed_gb_total': round(sum(costs), 4),
        'billed_gb_max': round(max(costs, default=0.0), 4),
        'seconds_mean': round(sum(r['seconds'] for r in results)
                              / max(1, len(results)), 3),
        'wrong': [{'text': r['row']['text'],
                   'expected': r['row']['expected_status'],
                   'got': r['status'], 'checks': r['checks']}
                  for r in results if not r['checks']['correct']],
    }


def run(bot: pipeline.Chatbot, rows: list[dict]) -> dict:
    """Answers every question and returns the summary and every result."""
    results = []
    for row in rows:
        start = time.perf_counter()
        out = bot.answer(row['text'])
        results.append({
            'row': row, 'status': out['status'], 'labels': out['labels'],
            'checks': check(row, out),
            'billed_gb': (out['budget_gb'] or {}).get('spent', 0.0),
            'seconds': round(time.perf_counter() - start, 3),
            'text': out['text']})
    return {'summary': summarize(results), 'results': results,
            'template_fingerprint': templates.templates_fingerprint(),
            'rules_fingerprint': guardrails.rules_fingerprint()}


def log(report: dict, bot: pipeline.Chatbot, dataset: str,
        e2e_path: pathlib.Path, model_dir: pathlib.Path) -> str:
    """Logs one run to the chatbot/answer-v1 experiment; returns its id."""
    s = report['summary']
    meta = json.loads((model_dir / 'meta.json').read_text())
    mlflow = tracking.setup(EXPERIMENT)
    with mlflow.start_run(run_name='template-answers-e2e') as active:
        tags = tracking.required_tags([e2e_path])
        tags['data_version'] = f'{tags["data_version"]}+{dataset}'
        mlflow.set_tags({**tags,
                         'rules_fingerprint': report['rules_fingerprint'],
                         'template_fingerprint':
                             report['template_fingerprint']})
        mlflow.log_params({
            'answer_writer': 'templates', 'dataset': dataset,
            'router_model_version': meta.get('model_version'),
            'router_params': json.dumps(meta.get('params')),
            'search_index_built': bot.index.meta.get('built_at', 'unknown'),
            'max_gb_per_question': bot.max_gb or 'default', 'n': s['n']})
        metrics = {'e2e_accuracy': s['accuracy'],
                   'e2e_billed_gb_total': s['billed_gb_total'],
                   'e2e_billed_gb_max': s['billed_gb_max'],
                   'e2e_seconds_mean': s['seconds_mean']}
        metrics.update({f'e2e_{k}': v for k, v in s['checks'].items()
                        if v is not None})
        metrics.update({f'e2e_accuracy_{k}': v['accuracy']
                        for k, v in s['by_expected_status'].items()
                        if v['accuracy'] is not None})
        mlflow.log_metrics(metrics)
        mlflow.log_dict(report, 'e2e_report.json')
    return active.info.run_id


def main(argv: list[str] | None = None) -> int:
    """Prints accuracy and every wrong answer."""
    parser = argparse.ArgumentParser(description='End-to-end chatbot check.')
    parser.add_argument('--dataset', default='vigil_md_dev')
    parser.add_argument('--e2e', type=pathlib.Path, default=E2E)
    parser.add_argument('--model-dir', type=pathlib.Path,
                        default=pipeline.DEFAULT_MODEL_DIR)
    parser.add_argument('--out', type=pathlib.Path, default=None)
    parser.add_argument('--no-log', action='store_true')
    args = parser.parse_args(argv)
    bot = pipeline.Chatbot.open(args.dataset, model_dir=args.model_dir)
    report = run(bot, generate.read_jsonl(args.e2e))
    s = report['summary']
    print(f'{s["n"]} questions on {args.dataset}: accuracy {s["accuracy"]}, '
          f'checks {s["checks"]}')
    print(f'by expected status: {s["by_expected_status"]}')
    print(f'billed {s["billed_gb_total"]} GB in total, at most '
          f'{s["billed_gb_max"]} GB per question, {s["seconds_mean"]} s mean')
    for w in s['wrong']:
        failed = [k for k in ('status', 'entity', 'refusal')
                  if not w['checks'][k]]
        print(f'  WRONG ({", ".join(failed)}): expected {w["expected"]}, '
              f'got {w["got"]}: {w["text"]}')
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, default=str))
    if not args.no_log:
        run_id = log(report, bot, args.dataset, args.e2e, args.model_dir)
        print(f'MLflow run {run_id} in {EXPERIMENT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
