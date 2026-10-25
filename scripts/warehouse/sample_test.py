"""End-to-end warehouse test on the sample records, in an isolated test setup.

Isolation (checked before any write; the script refuses to run otherwise):
  GCS       gs://vigil-md-test-nd            (production bucket is only READ: samples/)
  BigQuery  VIGIL_BQ_DATASET_SUFFIX=_test → vigil_md_raw_test, vigil_md_staging_test,
            vigil_md_prod_test, vigil_md_ops_test

Steps (run by dags/vigil_md_sample_test.py, or by hand in this order):
  reset      empty the test bucket, drop the 4 *_test datasets
  seed       wrap samples/{enforcement,recall,maude}_sample.ndjson as openFDA bulk
             zips, copy samples/gudid/gudid_sample_20261001.zip, in the raw/ layout
  (then create_tables.py and, per source, the full-load chain:
   unzip --scope all → load_raw --scope all --mode full → build_staging → merge_prod)
  check      counts per layer, load_log, GUDID fields, no temp tables / pending files
  scenarios  idempotency (same day again), content hash (same records, new zip),
             one changed record → exactly one prod update

Usage (inside the Airflow worker):
    VIGIL_BQ_DATASET_SUFFIX=_test python scripts/warehouse/sample_test.py reset
    VIGIL_BQ_DATASET_SUFFIX=_test python scripts/warehouse/sample_test.py seed
    ...
    python scripts/warehouse/sample_test.py check --print-only     # plan only, no GCP
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.warehouse import schemas

PROJECT     = "vigil-md-1792696816"
SRC_BUCKET  = "vigil-md-raw-nd"       # read only: samples/
TEST_BUCKET = "vigil-md-test-nd"
REPO        = Path(__file__).resolve().parents[2]
D1, D2, D3  = "2026-10-25", "2026-10-26", "2026-10-27"   # ingest dates (labels only)

EXPECTED_RAW = {"enforcement": 200, "recall": 200, "maude": 200, "gudid": 100}
ENF_ZIP = "raw/enforcement/{date}/enforcement/device-enforcement-0001-of-0001.json.zip"
ENF_MEMBER = "device-enforcement-0001-of-0001.json"


# ── safety ───────────────────────────────────────────────────────────────────

def assert_test_setup() -> None:
    """Refuse to run against anything but the test bucket and *_test datasets."""
    if schemas.DATASET_SUFFIX != "_test":
        raise SystemExit("Refusing: set VIGIL_BQ_DATASET_SUFFIX=_test")
    if not all(ds.endswith("_test") for ds in schemas.DATASETS.values()):
        raise SystemExit(f"Refusing: datasets are not *_test: {schemas.DATASETS}")
    if TEST_BUCKET == SRC_BUCKET:
        raise SystemExit("Refusing: test bucket equals the production bucket")


# ── pure helpers ─────────────────────────────────────────────────────────────

def openfda_zip(records: list, member: str, meta: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, json.dumps({"meta": meta or {"last_updated": "2026-10-20"},
                                       "results": records}))
    return buf.getvalue()


def seed_layout(records: dict) -> list[tuple[str, str, list]]:
    """[(gcs path, zip member, records)] in the real bulk layout. MAUDE → 2 quarters."""
    m = records["maude"]
    return [
        (ENF_ZIP.format(date="2026-10-20"), ENF_MEMBER, records["enforcement"]),
        ("raw/recall/2026-10-20/recall/device-recall-0001-of-0001.json.zip",
         "device-recall-0001-of-0001.json", records["recall"]),
        ("raw/event/2026-10-20/2024q1/device-event-0001-of-0001.json.zip",
         "device-event-0001-of-0001.json", m[:len(m) // 2]),
        ("raw/event/2026-10-20/2024q2/device-event-0001-of-0001.json.zip",
         "device-event-0001-of-0001.json", m[len(m) // 2:]),
    ]


# ── GCP helpers ──────────────────────────────────────────────────────────────

def clients():
    from google.cloud import bigquery, storage
    return storage.Client(project=PROJECT), bigquery.Client(project=PROJECT)


def q(bq, sql: str) -> list[dict]:
    return [dict(r.items()) for r in bq.query(sql).result()]


def run_script(script: str, *args: str) -> str:
    """Run a warehouse script with the same environment (keeps the _test suffix)."""
    cmd = [sys.executable, str(REPO / "scripts/warehouse" / script), *args]
    r = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, env=os.environ)
    print(f"  $ {script} {' '.join(args)} → exit {r.returncode}")
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-3000:])
        raise RuntimeError(f"{script} failed")
    return r.stdout


def full_load(source: str, day: str) -> str:
    """The vigil_md_full_load chain for one source, on the test bucket."""
    run_script("unzip_to_ndjson.py", "--bucket", TEST_BUCKET, "--source", source,
               "--scope", "all", "--ingest-date", day)
    run_script("load_raw_to_bq.py", "--project", PROJECT, "--bucket", TEST_BUCKET,
               "--source", source, "--scope", "all", "--ingest-date", day, "--mode", "full")
    run_script("build_staging.py", "--project", PROJECT, "--source", source, "--ingest-date", day)
    return run_script("merge_prod.py", "--project", PROJECT, "--source", source, "--ingest-date", day)


class Checks:
    def __init__(self):
        self.results: list[tuple[str, bool]] = []

    def __call__(self, name: str, ok, detail=""):
        self.results.append((name, bool(ok)))
        print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")

    def finish(self):
        failed = [n for n, ok in self.results if not ok]
        print(f"\n=== {len(self.results) - len(failed)}/{len(self.results)} checks passed ===")
        if failed:
            raise RuntimeError("Failed checks: " + "; ".join(failed))


def counts(bq, day: str) -> dict:
    out = {}
    for s in schemas.SOURCES:
        out[s] = {
            "raw": q(bq, f"SELECT COUNT(*) n FROM `{schemas.raw_table(PROJECT, s)}` WHERE _ingest_date = '{day}'")[0]["n"],
            "stg": q(bq, f"SELECT COUNT(*) n FROM `{schemas.staging_table(PROJECT, s)}` WHERE _ingest_date = '{day}'")[0]["n"],
            "prod": q(bq, f"SELECT COUNT(*) n FROM `{schemas.prod_table(PROJECT, s)}`")[0]["n"],
        }
    return out


# ── steps ────────────────────────────────────────────────────────────────────

def step_reset() -> None:
    gcs, bq = clients()
    from google.api_core.exceptions import NotFound
    try:
        bucket = gcs.get_bucket(TEST_BUCKET)
        blobs = list(gcs.list_blobs(TEST_BUCKET))
        for b in blobs:
            b.delete()
        print(f"  emptied gs://{bucket.name} ({len(blobs)} objects)")
    except NotFound:
        gcs.create_bucket(TEST_BUCKET, location=schemas.LOCATION)
        print(f"  created gs://{TEST_BUCKET}")
    for ds in schemas.DATASETS.values():
        assert ds.endswith("_test")
        bq.delete_dataset(f"{PROJECT}.{ds}", delete_contents=True, not_found_ok=True)
        print(f"  dropped {PROJECT}.{ds}")


def step_seed() -> None:
    gcs, _ = clients()
    src, test = gcs.bucket(SRC_BUCKET), gcs.bucket(TEST_BUCKET)
    records = {}
    for s in ("enforcement", "recall", "maude"):
        text = src.blob(f"samples/{s}_sample.ndjson").download_as_text()
        records[s] = [json.loads(l) for l in text.splitlines() if l.strip()]
    for path, member, recs in seed_layout(records):
        test.blob(path).upload_from_string(openfda_zip(recs, member), content_type="application/zip")
        print(f"  seeded {path} ({len(recs)} records)")
    src.copy_blob(src.blob("samples/gudid/gudid_sample_20261001.zip"), test,
                  "raw/gudid/20261001/gudid_sample_20261001.zip")
    print("  seeded raw/gudid/20261001/gudid_sample_20261001.zip (100 devices)")


def step_check() -> None:
    gcs, bq = clients()
    check = Checks()
    c = counts(bq, D1)
    print("  counts:", json.dumps(c))
    for s in schemas.SOURCES:
        check(f"raw {s} = {EXPECTED_RAW[s]} records", c[s]["raw"] == EXPECTED_RAW[s], c[s])
        check(f"prod {s} = staging {s}", c[s]["prod"] == c[s]["stg"] > 0, c[s])
    raw_e = schemas.raw_table(PROJECT, "enforcement")
    uniq = q(bq, f"SELECT COUNT(DISTINCT JSON_VALUE(payload,'$.recall_number')) n FROM `{raw_e}` WHERE _ingest_date='{D1}'")[0]["n"]
    check("enforcement staging = distinct recall_number", c["enforcement"]["stg"] == uniq, uniq)
    log = {r["source"]: r["files"] for r in q(bq, f"SELECT source, COUNT(*) files FROM `{schemas.load_log_table(PROJECT)}` GROUP BY source")}
    check("load_log: enforcement 1, recall 1, maude 2, gudid 1 files",
          log == {"enforcement": 1, "recall": 1, "maude": 2, "gudid": 1}, log)
    g = q(bq, f"""SELECT COUNTIF(udi_di IS NOT NULL) udi, COUNTIF(ARRAY_LENGTH(product_codes) > 0) pc,
                  COUNTIF(manufacturer_name_norm IS NOT NULL) mfr, COUNTIF(publish_date IS NOT NULL) pub
                  FROM `{schemas.prod_table(PROJECT, 'gudid')}`""")[0]
    check("gudid prod: Primary DI, product codes, manufacturer, publish_date filled", min(g.values()) > 0, g)
    tmp = q(bq, f"SELECT COUNT(*) n FROM `{PROJECT}.{schemas.DATASETS['ops']}.INFORMATION_SCHEMA.TABLES` "
                "WHERE STARTS_WITH(table_name, '_tmp_')")[0]["n"]
    check("no leftover _tmp_ tables", tmp == 0, tmp)
    pend = [b.name for b in gcs.list_blobs(TEST_BUCKET, prefix="manifests/pending/")]
    check("no pending manifests left", not pend, pend)
    parts = sorted(b.name for b in gcs.list_blobs(TEST_BUCKET, prefix="manifests/partitions/"))
    check("partition state for all 4 sources",
          parts == [f"manifests/partitions/{s}.json" for s in sorted(schemas.SOURCES)], parts)
    check.finish()


def step_scenarios() -> None:
    gcs, bq = clients()
    src, test = gcs.bucket(SRC_BUCKET), gcs.bucket(TEST_BUCKET)
    check = Checks()
    before = counts(bq, D1)
    log_table = schemas.load_log_table(PROJECT)
    log_before = q(bq, f"SELECT COUNT(*) n FROM `{log_table}`")[0]["n"]

    print("== idempotency: same files, same day, partition state wiped ==")
    for b in gcs.list_blobs(TEST_BUCKET, prefix="manifests/partitions/"):
        b.delete()
    merged = {s: full_load(s, D1) for s in schemas.SOURCES}
    check("raw/staging/prod counts unchanged", counts(bq, D1) == before)
    check("load_log has no new rows", q(bq, f"SELECT COUNT(*) n FROM `{log_table}`")[0]["n"] == log_before)
    check("second MERGE changes 0 rows in every source",
          all("→ 0 inserted or updated" in out for out in merged.values()))

    print("== content hash: same records, new export date, different JSON formatting ==")
    recs = [json.loads(l) for l in src.blob("samples/enforcement_sample.ndjson").download_as_text().splitlines() if l.strip()]
    test.blob(ENF_ZIP.format(date="2026-10-27")).upload_from_string(
        openfda_zip([dict(reversed(list(r.items()))) for r in recs], ENF_MEMBER,
                    meta={"last_updated": "2026-10-27"}))
    out = run_script("unzip_to_ndjson.py", "--bucket", TEST_BUCKET, "--source", "enforcement",
                     "--scope", "all", "--ingest-date", D2)
    check("unzip marks the new zip skip_load", "1 unchanged (skip_load)" in out)
    run_script("load_raw_to_bq.py", "--project", PROJECT, "--bucket", TEST_BUCKET, "--source", "enforcement",
               "--scope", "all", "--ingest-date", D2, "--mode", "full")
    raw_e = schemas.raw_table(PROJECT, "enforcement")
    check("no raw rows loaded for unchanged content",
          q(bq, f"SELECT COUNT(*) n FROM `{raw_e}` WHERE _ingest_date='{D2}'")[0]["n"] == 0)

    print("== one changed record ==")
    changed = json.loads(json.dumps(recs))
    key = changed[0]["recall_number"]
    changed[0]["reason_for_recall"] = "SAMPLE TEST: changed reason"
    test.blob(ENF_ZIP.format(date="2026-10-28")).upload_from_string(openfda_zip(changed, ENF_MEMBER))
    out = full_load("enforcement", D3)
    check("MERGE updates exactly 1 row", "→ 1 inserted or updated" in out)
    prod_e = schemas.prod_table(PROJECT, "enforcement")
    row = q(bq, f"SELECT reason_text, first_loaded_at < last_updated_at AS bumped FROM `{prod_e}` "
                f"WHERE source_record_id = '{key}'")
    check("prod row has the new value and a newer last_updated_at",
          len(row) == 1 and row[0]["reason_text"] == "SAMPLE TEST: changed reason" and row[0]["bumped"], row)
    check("prod enforcement row count unchanged",
          q(bq, f"SELECT COUNT(*) n FROM `{prod_e}`")[0]["n"] == before["enforcement"]["prod"])
    check("no duplicate keys in prod",
          q(bq, f"SELECT COUNT(*) - COUNT(DISTINCT source_record_id) d FROM `{prod_e}`")[0]["d"] == 0)
    check.finish()


STEPS = {"reset": step_reset, "seed": step_seed, "check": step_check, "scenarios": step_scenarios}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=list(STEPS) + ["all"])
    parser.add_argument("--print-only", action="store_true", help="Print the plan, then exit. No GCP calls.")
    args = parser.parse_args()

    if args.print_only:
        print(f"sample_test {args.step}: bucket gs://{TEST_BUCKET}, datasets {sorted(schemas.DATASETS.values())}")
        print(f"  suffix ok: {schemas.DATASET_SUFFIX == '_test'} (VIGIL_BQ_DATASET_SUFFIX={schemas.DATASET_SUFFIX!r})")
        return

    assert_test_setup()
    if args.step == "all":
        step_reset(); step_seed()
        run_script("create_tables.py", "--project", PROJECT)
        for s in schemas.SOURCES:
            full_load(s, D1)
        step_check(); step_scenarios()
    else:
        STEPS[args.step]()


if __name__ == "__main__":
    main()
