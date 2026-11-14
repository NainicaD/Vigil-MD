"""Dry-run the query layer for one entity and print the bytes each query would read.

Free: dry runs only, nothing is read or billed. Each lookup reads one partition of a summary
table; narrative_texts depends on step 1's results, so it is estimated for --months recent
months (one report date per month).

Usage (from Model-Development/chatbot/):
    python -m query_layer.estimate --entity-type product_code --entity-id LZG
    python -m query_layer.estimate --entity-type device --entity-id dev:f0cdc15b4d63
    python -m query_layer.estimate --entity-type manufacturer --entity-id respironics --dataset vigil_md_dev
"""
from __future__ import annotations

import argparse
import datetime as dt

from . import config, queries
from .backend import BigQueryBackend


def plan(backend: BigQueryBackend, entity_type: str, entity_id: str, months: int) -> list[queries.Query]:
    t = backend.table
    qs = [queries.catalog(t, entity_type, entity_id), queries.counts(t, entity_type, entity_id),
          queries.problems(t, entity_type, entity_id), queries.recalls(t, entity_type, entity_id),
          queries.narrative_candidates(t, entity_type, entity_id)]
    first = dt.date.today().replace(day=1)
    dates = []
    for _ in range(months):
        first = (first - dt.timedelta(days=1)).replace(day=1)
        dates.append(first.isoformat())
    qs.append(queries.narrative_texts(t, ["0"], dates))
    if entity_type == "device":
        qs.append(queries.device_details(t, entity_id))
    return qs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--entity-type", required=True, choices=config.ENTITY_TYPES)
    ap.add_argument("--entity-id", required=True)
    ap.add_argument("--dataset", default=config.DATASET)
    ap.add_argument("--months", type=int, default=3)
    a = ap.parse_args(argv)
    backend = BigQueryBackend(dataset=a.dataset)
    qs = plan(backend, a.entity_type, a.entity_id, a.months)
    est = dict(zip((q.name for q in qs), backend.estimate(qs)))
    summary = sum(est[n] for n in ("catalog", "counts", "problems", "recalls"))
    narr = est["narrative_candidates"] + est["narrative_texts"]
    print(f"{a.dataset}  {a.entity_type}={a.entity_id!r}")
    for name, b in est.items():
        print(f"  {name:22} {b / 1e6:9.2f} MB")
    print(f"  {'device summary':22} {summary / 1e6:9.2f} MB   (always run)")
    print(f"  {'narratives':22} {narr / 1e6:9.2f} MB   (when asked)")
    print(f"  question cap: {config.MAX_GB_PER_QUESTION} GB; BigQuery bills at least 10 MB per table read")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
