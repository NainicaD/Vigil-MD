"""Validate the staging tables against the expected schema.

Step 1 (this version): compare each staging table's live schema (BigQuery
table metadata, no data scanned, no cost) with schemas.py.

Usage:
    python scripts/validation/run_validation.py --project vigil-md-1792696816
    python scripts/validation/run_validation.py --project ... --source recall

Exit codes: 0 = all match, 3 = schema differences found (a data failure,
not worth retrying), anything else = unexpected error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger                       # noqa: E402
from scripts.validation.expectations import compare_schema, expected_schema  # noqa: E402
from scripts.warehouse import schemas                       # noqa: E402

log = get_logger("run_validation")
EXIT_OK, EXIT_DATA_FAIL = 0, 3


def live_schema(client, table_id: str) -> list[tuple[str, str, str]]:
    """(name, type, mode) from BigQuery table metadata. Reads no rows."""
    table = client.get_table(table_id)
    return [(f.name, f.field_type, f.mode) for f in table.schema]


def check_schemas(client, project: str, sources: list[str]) -> bool:
    """Log PASS/FAIL per source. Return True if every schema matches."""
    ok = True
    for source in sources:
        table_id = schemas.staging_table(project, source)
        problems = compare_schema(expected_schema(source), live_schema(client, table_id))
        if problems:
            ok = False
            log.error("schema FAIL %s (%d differences)", table_id, len(problems))
            for p in problems:
                log.error("  %s", p)
        else:
            log.info("schema PASS %s (%d columns)", table_id, len(expected_schema(source)))
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--source", choices=schemas.SOURCES,
                    help="one source (default: all four)")
    args = ap.parse_args()

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    sources = [args.source] if args.source else schemas.SOURCES
    sys.exit(EXIT_OK if check_schemas(client, args.project, sources) else EXIT_DATA_FAIL)


if __name__ == "__main__":
    main()
