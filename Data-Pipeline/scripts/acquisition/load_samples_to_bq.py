"""Load NDJSON sample files from GCS into BigQuery tables.

Usage:
    python scripts/acquisition/load_samples_to_bq.py \
        --project vigil-md-1792696816 \
        --bucket vigil-md-raw-nd
"""
import argparse
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("load_bq")

SOURCES = ["enforcement", "recall", "maude", "gudid"]
DATASET = "vigil_md_raw"


def strip_empty(obj):
    """Remove empty dicts and lists that break BigQuery autodetect."""
    if isinstance(obj, dict):
        return {k: strip_empty(v) for k, v in obj.items()
                if v not in ({}, [], None, "")}
    if isinstance(obj, list):
        return [strip_empty(i) for i in obj if i not in ({}, [], None, "")]
    return obj


def clean_ndjson(source: str) -> Path:
    src = Path("data/raw/samples") / f"{source}_sample.ndjson"
    dst = Path("data/raw/samples") / f"{source}_sample_clean.ndjson"
    with open(src) as fin, open(dst, "w") as fout:
        for line in fin:
            record = json.loads(line)
            cleaned = strip_empty(record)
            fout.write(json.dumps(cleaned) + "\n")
    log.info("Cleaned %s → %s", src.name, dst.name)
    return dst


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()

    from google.cloud import bigquery, storage

    storage_client = storage.Client()
    bucket = storage_client.bucket(args.bucket)

    client = bigquery.Client(project=args.project)
    dataset_id = f"{args.project}.{DATASET}"
    dataset = bigquery.Dataset(dataset_id)
    dataset.location = "US"
    client.create_dataset(dataset, exists_ok=True)
    log.info("Dataset ready: %s", dataset_id)

    for source in SOURCES:
        clean_path = clean_ndjson(source)
        blob_name = f"samples/{clean_path.name}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(clean_path))
        uri = f"gs://{args.bucket}/{blob_name}"
        log.info("Uploaded cleaned → %s", uri)

        table_id = f"{dataset_id}.{source}_sample"
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
            autodetect=True,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        )

        log.info("Loading %s → %s …", uri, table_id)
        job = client.load_table_from_uri(uri, table_id, job_config=job_config)
        job.result()

        table = client.get_table(table_id)
        log.info("  Loaded %d rows, %d columns",
                 table.num_rows, len(table.schema))

    print("\n=== All tables loaded. Run these in BigQuery console: ===")
    print(f"SELECT * FROM `{args.project}.{DATASET}.enforcement_sample` LIMIT 5;")
    print(f"SELECT * FROM `{args.project}.{DATASET}.recall_sample` LIMIT 5;")
    print(f"SELECT * FROM `{args.project}.{DATASET}.maude_sample` LIMIT 5;")
    print(f"SELECT * FROM `{args.project}.{DATASET}.gudid_sample` LIMIT 5;")


if __name__ == "__main__":
    main()