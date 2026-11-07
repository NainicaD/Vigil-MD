"""Dry-run the query layer for one device key and print the bytes each query would scan.

Free: dry runs only, nothing is read or billed. Use it to set config.MAX_GB_PER_QUESTION
(about 2-3 times the largest normal question) and to see what daily summary tables would save.

narrative_texts depends on step 1's results, so it is estimated for --months recent months
(one report date per month), the usual spread of the newest reports.

Usage (from Model-Development/chatbot/):
    python -m query_layer.estimate --key-type product_code --key LZG
    python -m query_layer.estimate --key-type brand --key "MiniMed 780G" --dataset vigil_md_prod
"""
from __future__ import annotations

import argparse
import datetime as dt

from . import config, queries
from .backend import BigQueryBackend


def plan(backend: BigQueryBackend, key_type: str, key: str, months: int) -> list[queries.Query]:
    t = backend.table
    qs = [queries.counts(t, key_type, key), queries.problems(t, key_type, key)]
    if key_type in queries.RECALL_FILTER:
        qs.append(queries.recalls(t, key_type, key))
    qs.append(queries.narrative_candidates(t, key_type, key))
    first = dt.date.today().replace(day=1)
    dates = []
    for _ in range(months):
        first = (first - dt.timedelta(days=1)).replace(day=1)
        dates.append(first.isoformat())
    qs.append(queries.narrative_texts(t, ["0"], dates))
    if key_type in queries.GUDID_FILTER:
        qs.append(queries.identity(t, key_type, key))
    return qs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--key-type", required=True, choices=config.KEY_TYPES)
    ap.add_argument("--key", required=True)
    ap.add_argument("--dataset", default=config.DATASET)
    ap.add_argument("--months", type=int, default=3)
    a = ap.parse_args(argv)
    backend = BigQueryBackend(dataset=a.dataset)
    qs = plan(backend, a.key_type, a.key, a.months)
    est = dict(zip((q.name for q in qs), backend.estimate(qs)))
    summary = sum(est[n] for n in ("counts", "problems", "recalls") if n in est)
    narr = est["narrative_candidates"] + est["narrative_texts"]
    print(f"{a.dataset}  {a.key_type}={a.key!r}")
    for name, b in est.items():
        print(f"  {name:22} {b / 1e9:8.3f} GB")
    print(f"  {'device summary':22} {summary / 1e9:8.3f} GB   (always run)")
    print(f"  {'narratives':22} {narr / 1e9:8.3f} GB   (when asked)")
    print(f"  question cap now: {config.MAX_GB_PER_QUESTION} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
