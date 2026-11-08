"""Download label_set_v1 from BigQuery ONCE and save it locally.

Uses list_rows (the row-listing API), not a SQL query, so it does not use the team's query
budget. A SELECT ... LIMIT would still bill for scanning the whole table.

Usage (from anywhere):
    python pipeline/s03_export_label_set.py --project <id> --schema-only   # free, downloads nothing
    python pipeline/s03_export_label_set.py --project <id>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model.bq_utils import MODEL_DATASET  # noqa: E402

EXIT_OK, EXIT_CONFIG = 0, 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Save label_set_v1 locally (no query cost)")
    p.add_argument("--project", required=True)
    p.add_argument("--table", default="label_set_v1", help=f"table in {MODEL_DATASET}")
    p.add_argument("--out", default=ROOT / "data" / "label_set_v1.parquet", type=Path)
    p.add_argument("--schema-only", action="store_true", help="print columns and size, download nothing")
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)

    from google.cloud import bigquery

    if args.out.exists() and not args.schema_only and not args.overwrite:
        print(f"{args.out} exists. Use --overwrite to replace it.")
        return EXIT_CONFIG

    client = bigquery.Client(project=args.project)
    name = f"{args.project}.{MODEL_DATASET}.{args.table}"
    table = client.get_table(name)   # metadata only, free

    print(f"{name}: {table.num_rows:,} rows, {table.num_bytes / 1e6:.0f} MB")
    for field in table.schema:
        print(f"  {field.name:<40} {field.field_type}")
    if args.schema_only:
        return EXIT_OK

    print("\ndownloading every row (no query cost; may take a few minutes)...")
    # create_bqstorage_client=False keeps this on the free tabledata.list path
    df = client.list_rows(table).to_dataframe(create_bqstorage_client=False)
    assert len(df) == table.num_rows, f"got {len(df)} rows, expected {table.num_rows}"

    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(f"saved {len(df):,} rows to {args.out}")
    print(df["meta_split"].value_counts().to_string())
    print(df["label"].value_counts().to_string())
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
