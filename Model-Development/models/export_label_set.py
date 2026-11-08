"""Download label_set_v1 from BigQuery ONCE and save it locally.

Uses list_rows (the row-listing API), not a SQL query, so it does not use the team's query budget.
A SELECT ... LIMIT would still bill for scanning the whole table.

Usage (from Model-Development/models):
    python export_label_set.py --schema-only    # print columns and size, downloads nothing
    python export_label_set.py                  # save the whole table to label_set_v1.parquet
"""
import argparse

from google.cloud import bigquery

TABLE = "vigil-md-1792696816.vigil_md_model.label_set_v1"

parser = argparse.ArgumentParser(description="Save label_set_v1 locally")
parser.add_argument("--schema-only", action="store_true")
parser.add_argument("--out", default="label_set_v1.parquet")
args = parser.parse_args()

client = bigquery.Client(project="vigil-md-1792696816")
table = client.get_table(TABLE)   # metadata only, free

print(f"{TABLE}: {table.num_rows:,} rows, {table.num_bytes / 1e6:.0f} MB")
for field in table.schema:
    print(f"  {field.name:<40} {field.field_type}")

if args.schema_only:
    raise SystemExit

print("\ndownloading every row (no query cost; may take a few minutes)...")
df = client.list_rows(table).to_dataframe(create_bqstorage_client=False)
assert len(df) == table.num_rows, f"got {len(df)} rows, expected {table.num_rows}"

df.to_parquet(args.out, index=False)
print(f"saved {len(df):,} rows to {args.out}")
print(df["meta_split"].value_counts().to_string())
print(df["label"].value_counts().to_string())
