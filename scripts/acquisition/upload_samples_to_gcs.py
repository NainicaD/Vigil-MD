"""Convert sample JSON files to NDJSON and upload to GCS.

BigQuery requires one JSON object per line (NDJSON).
Our sample files have a {"records": [...]} wrapper which we strip here.

Usage:
    python scripts/acquisition/upload_samples_to_gcs.py \
        --bucket vigil-md-raw-nd
"""
import argparse
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("upload_samples")

SAMPLES = Path("data/raw/samples")
SOURCES = ["enforcement", "recall", "maude", "gudid"]


def to_ndjson(source: str) -> Path:
    src = SAMPLES / f"{source}_sample.json"
    dst = SAMPLES / f"{source}_sample.ndjson"

    data = json.loads(src.read_text())
    records = data.get("records", [])

    with open(dst, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    log.info("Converted %s: %d records → %s", source, len(records), dst)
    return dst


def upload(local_path: Path, bucket: str) -> str:
    from google.cloud import storage
    client = storage.Client()
    b = client.bucket(bucket)
    blob_name = f"samples/{local_path.name}"
    blob = b.blob(blob_name)
    blob.upload_from_filename(str(local_path))
    uri = f"gs://{bucket}/{blob_name}"
    log.info("Uploaded → %s", uri)
    return uri


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args()

    uris = {}
    for source in SOURCES:
        ndjson = to_ndjson(source)
        uri = upload(ndjson, args.bucket)
        uris[source] = uri

    print("\n=== GCS URIs (use these for BigQuery load) ===")
    for source, uri in uris.items():
        print(f"  {source}: {uri}")


if __name__ == "__main__":
    main()