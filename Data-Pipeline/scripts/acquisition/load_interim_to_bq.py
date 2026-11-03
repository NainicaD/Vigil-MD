"""Upload interim Parquet files to GCS and load into BigQuery.

Creates a second dataset vigil_md_processed with the cleaned,
flattened tables ready for entity resolution.

Usage:
    python scripts/acquisition/load_interim_to_bq.py \
        --project vigil-md-1792696816 \
        --bucket vigil-md-raw-nd
"""
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("load_interim")

INTERIM = Path("data/interim")
DATASET = "vigil_md_processed"
SOURCES = ["enforcement", "recall", "maude", "gudid"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()

    required = [INTERIM / f"{s}_flat.parquet" for s in SOURCES]
    required.append(INTERIM / "links_maude_gudid.parquet")
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(
            f"Missing interim files (run flatten_to_parquet.py first): {missing}")

    from google.cloud import bigquery, storage

    # ── Upload Parquet files to GCS ───────────────────────────────────────────
    storage_client = storage.Client()
    bucket = storage_client.bucket(args.bucket)

    log.info("Uploading interim Parquet files to GCS …")
    uris = {}
    for source in SOURCES:
        local = INTERIM / f"{source}_flat.parquet"
        blob_name = f"interim/{source}_flat.parquet"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(local))
        uri = f"gs://{args.bucket}/{blob_name}"
        uris[source] = uri
        log.info("Uploaded → %s", uri)

    # Upload linking outputs too
    for name in ["links_maude_gudid", "linking_report"]:
        ext = ".parquet" if "links" in name else ".json"
        local = INTERIM / f"{name}{ext}"
        if local.exists():
            blob_name = f"interim/{name}{ext}"
            bucket.blob(blob_name).upload_from_filename(str(local))
            log.info("Uploaded → gs://%s/%s", args.bucket, blob_name)

    # ── Create BigQuery dataset ───────────────────────────────────────────────
    client = bigquery.Client(project=args.project)
    dataset_id = f"{args.project}.{DATASET}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "US"
    client.create_dataset(dataset, exists_ok=True)
    log.info("Dataset ready: %s", dataset_id)

    # ── Load Parquet files into BigQuery ─────────────────────────────────────
    for source, uri in uris.items():
        table_id = f"{dataset_id}.{source}_flat"
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )
        log.info("Loading %s → %s …", uri, table_id)
        job = client.load_table_from_uri(uri, table_id, job_config=job_config)
        job.result()
        table = client.get_table(table_id)
        if not table.num_rows:
            raise RuntimeError(f"{table_id} loaded 0 rows")
        log.info("  Loaded %d rows, %d columns",
                 table.num_rows, len(table.schema))

    # Load links table
    links_uri = f"gs://{args.bucket}/interim/links_maude_gudid.parquet"
    links_table = f"{dataset_id}.links_maude_gudid"
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    log.info("Loading links_maude_gudid …")
    job = client.load_table_from_uri(
        links_uri, links_table, job_config=job_config)
    job.result()
    table = client.get_table(links_table)
    if not table.num_rows:
        raise RuntimeError(f"{links_table} loaded 0 rows")
    log.info("  Loaded %d rows, %d columns", table.num_rows, len(table.schema))

    print("\n=== All processed tables loaded into BigQuery ===")
    print(f"\nDataset: {dataset_id}")
    print("\nTables:")
    for source in SOURCES:
        print(f"  {source}_flat")
    print("  links_maude_gudid")
    print("\nRun these queries in BigQuery console:")
    print(f"""
-- Record counts
SELECT 'enforcement' AS source, COUNT(*) AS n 
FROM `{args.project}.{DATASET}.enforcement_flat`
UNION ALL
SELECT 'recall', COUNT(*) FROM `{args.project}.{DATASET}.recall_flat`
UNION ALL
SELECT 'maude', COUNT(*) FROM `{args.project}.{DATASET}.maude_flat`
UNION ALL
SELECT 'gudid', COUNT(*) FROM `{args.project}.{DATASET}.gudid_flat`;

-- Check normalization
SELECT manufacturer_name_raw, manufacturer_name_norm, COUNT(*) as n
FROM `{args.project}.{DATASET}.enforcement_flat`
GROUP BY 1, 2
ORDER BY n DESC
LIMIT 10;

-- Check MAUDE-GUDID links
SELECT * FROM `{args.project}.{DATASET}.links_maude_gudid`
LIMIT 10;
    """)


if __name__ == "__main__":
    main()