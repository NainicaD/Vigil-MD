"""Asks the chatbot from the command line (from Model-Development/chatbot/).

  VIGIL_BQ_DATASET=vigil_md_dev python -m answer "Was DreamStation recalled?"
  python -m answer --json "How many deaths for product code LZG?"
"""

import argparse
import json

from answer import pipeline


def main(argv: list[str] | None = None) -> int:
    """Prints the answer to one question."""
    parser = argparse.ArgumentParser(
        description='Ask Vigil MD a question (template answers, no LLM).')
    parser.add_argument('question')
    parser.add_argument('--dataset', default=None,
                        help='BigQuery dataset and search index (default '
                        'VIGIL_BQ_DATASET, else vigil_md_prod)')
    parser.add_argument('--max-gb', type=float, default=None,
                        help='cost cap for this question')
    parser.add_argument('--json', action='store_true',
                        help='print the full result as JSON')
    args = parser.parse_args(argv)
    bot = pipeline.Chatbot.open(args.dataset, max_gb=args.max_gb)
    out = bot.answer(args.question)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    spent = (out['budget_gb'] or {}).get('spent', 0)
    print(out['text'])
    print(f'\n[{out["status"]}; {len(out["sources"])} queries, {spent} GB '
          f'billed; templates {out["template_fingerprint"]}, rules '
          f'{out["rules_fingerprint"]}]')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
