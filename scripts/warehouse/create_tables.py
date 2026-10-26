"""Create the warehouse datasets and tables (RAW, STAGING, PROD, OPS).

Safe to run again: every create uses exists_ok=True. It never changes
or drops an existing table. If an existing table has different columns
than scripts/warehouse/schemas.py, it raises instead of continuing.

Usage:
    python scripts/warehouse/create_tables.py --project vigil-md-1792696816

    # Print the plan only. Creates no client, makes no GCP call:
    python scripts/warehouse/create_tables.py --project vigil-md-1792696816 --print-only
"""
from __future__ import annotations

import argparse
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
from scripts.warehouse import schemas
log = get_logger("create_tables")


# ── Plan rendering ────────────────────────────────────────────────────────────

def describe(spec: dict) -> str:
    """Return a readable description of one table spec. Pure function."""
    part = spec["partition"]
    part_txt = f"{part[0]} ({part[1]})" if part else "none"
    lines = [
        spec["table_id"],
        f"  partition: {part_txt}"
        + ("  [partition filter required]" if spec["require_partition_filter"] else ""),
        f"  cluster:   {', '.join(spec['cluster'])}",
        "  columns:",
    ]
    for name, typ, mode in spec["columns"]:
        lines.append(f"    {name:<24} {typ:<10} {mode}")
    return "\n".join(lines)


# ── BigQuery ──────────────────────────────────────────────────────────────────

def build_table(spec: dict):
    from google.cloud import bigquery

    table = bigquery.Table(
        spec["table_id"],
        schema=[bigquery.SchemaField(n, t, mode=m) for n, t, m in spec["columns"]],
    )
    if spec["partition"]:
        field, unit = spec["partition"]
        table.time_partitioning = bigquery.TimePartitioning(
            type_=getattr(bigquery.TimePartitioningType, unit), field=field)
        table.require_partition_filter = spec["require_partition_filter"]
    table.clustering_fields = spec["cluster"]
    return table


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--print-only", action="store_true",
                        help="Print datasets and tables, then exit. No GCP calls.")
    args = parser.parse_args()

    specs = schemas.table_specs(args.project)

    if args.print_only:
        print("=== Datasets (location %s) ===" % schemas.LOCATION)
        for ds in schemas.DATASETS.values():
            print(f"  {args.project}.{ds}")
        print(f"\n=== Tables ({len(specs)}) ===")
        for spec in specs:
            print(describe(spec) + "\n")
        return

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)

    for ds in schemas.DATASETS.values():
        dataset = bigquery.Dataset(f"{args.project}.{ds}")
        dataset.location = schemas.LOCATION
        client.create_dataset(dataset, exists_ok=True)
        log.info("Dataset ready: %s.%s", args.project, ds)

    for spec in specs:
        table = client.create_table(build_table(spec), exists_ok=True)
        expected = [n for n, _, _ in spec["columns"]]
        actual   = [f.name for f in table.schema]
        if actual != expected:
            raise RuntimeError(
                f"{spec['table_id']} exists with different columns.\n"
                f"  expected: {expected}\n  actual:   {actual}")
        log.info("Table ready: %s", spec["table_id"])

    print(f"\n=== {len(specs)} warehouse tables ready ===")


if __name__ == "__main__":
    main()
