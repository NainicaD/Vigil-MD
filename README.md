# Vigil MD

**ML-based entity resolution across FDA medical device databases.**

Vigil MD links medical device records from multiple FDA sources into one unified, evidence-linked database. It identifies when records from different sources describe the same manufacturer, device model, or recall, while keeping genuinely different devices and recalls separate. Users can then search a device's or manufacturer's recall history through a chat interface backed by the unified database.

Inspired by Kramer et al. (2012), *Security and Privacy Qualities of Medical Devices: An Analysis of FDA Postmarket Surveillance*, PLoS ONE.

---

## Data Sources

| Source | URL | Role | Update cadence |
|---|---|---|---|
| FDA Enforcement | https://open.fda.gov/apis/device/enforcement/ | Recall actions and classification | Weekly |
| Device Recall | https://open.fda.gov/apis/device/recall/ | Recall details and corrective actions | Weekly |
| MAUDE | https://open.fda.gov/apis/device/event/ | Adverse event and malfunction reports | Quarterly |
| GUDID | https://accessgudid.nlm.nih.gov/download | Canonical device identifiers | Monthly |

Current scope: all device types, MAUDE 2016–2026. Enforcement, recall and GUDID are loaded in full.

---

## Project Structure

```
Vigil-MD/
├── dags/
│   ├── pull_samples_dag.py          # development DAG — 200 sample records
│   ├── vigil_md_acquisition.py      # daily: FDA + GUDID → GCS, then starts vigil_md_load
│   ├── vigil_md_load.py             # triggered: GCS → RAW → STAGING → PROD (BigQuery)
│   └── vigil_md_full_load.py        # manual: first load, backfill, rebuild
├── scripts/
│   ├── acquisition/
│   │   ├── pull_samples.py          # pull 200 sample records per source
│   │   ├── fetch_sources.py         # bulk download → GCS (--mode incremental | full)
│   │   ├── gudid_convert.py         # GUDID: download once, keep zip, XML → NDJSON locally
│   │   ├── upload_samples_to_gcs.py # upload sample NDJSON to GCS
│   │   ├── load_samples_to_bq.py    # load raw samples into BigQuery (sample tables)
│   │   └── load_interim_to_bq.py    # load processed sample Parquet into BigQuery
│   ├── warehouse/                   # RAW → STAGING → PROD (see "GCS → BigQuery pipeline")
│   │   ├── schemas.py               # every warehouse table: columns, partitions, keys
│   │   ├── create_tables.py         # create the datasets and tables (safe to re-run)
│   │   ├── convert.py               # zip → {"payload": record} NDJSON (openFDA JSON, GUDID XML)
│   │   ├── common.py                # shared GCS paths and pending-manifest helpers
│   │   ├── unzip_to_ndjson.py       # step 1: zip in GCS → NDJSON in GCS
│   │   ├── load_raw_to_bq.py        # step 2: NDJSON → RAW (idempotent, load_log)
│   │   ├── build_staging.py         # step 3: RAW → STAGING (flatten, cast, dedup)
│   │   ├── merge_prod.py            # step 4: STAGING → PROD (MERGE / --rebuild)
│   │   └── sql/staging_*.sql        # per-source staging SQL (port of flatten_to_parquet.py)
│   ├── preprocessing/
│   │   ├── schema_map.py            # unified field mapping for all sources
│   │   └── flatten_to_parquet.py    # normalize and flatten sample data to Parquet
│   ├── matching/
│   │   └── exact_linking.py         # UDI and recall-number exact joins
│   ├── analysis/
│   │   ├── bias_slices.py           # read-only bias slice analysis of PROD (see "Bias slice analysis")
│   │   └── bias_mitigation.py       # reweighting mitigation + Fairlearn check (see "Bias slice analysis")
│   └── logger.py                    # console + Logstash logger
├── tests/                           # pytest (all GCP calls mocked) — see tests/TEST_CASES.md
├── docs/
│   ├── WAREHOUSE_PLAN.md            # warehouse design, decisions, and the go-live Runbook
│   ├── TESTS_PLAN.md                # test plan for the warehouse code
│   ├── BIAS_REPORT.md               # bias slice analysis: results, limitations, proposed mitigations
│   └── issue.md                     # known issues and limitations
├── data/                            # DVC-tracked (not in Git)
│   ├── raw/samples/                 # 200-record sample JSON files
│   └── interim/                     # flattened Parquet + linking outputs; bias/ = bias analysis CSVs (git-ignored)
├── docker-compose.yaml              # Airflow with CeleryExecutor
├── docker-compose.elk.yml           # Elasticsearch, Logstash, Kibana
├── .env.example                     # environment variable template
└── requirements.txt
```

---

## GCP Resources

| Resource | Name | Used by |
|---|---|---|
| Project | `vigil-md-1792696816` | everything |
| GCS bucket | `vigil-md-raw-nd` | raw zips, NDJSON, manifests |
| BigQuery `vigil_md_raw` | `*_raw` tables (warehouse) and `*_sample` tables (samples) | RAW layer |
| BigQuery `vigil_md_staging` | `*_stg` tables | STAGING layer |
| BigQuery `vigil_md_prod` | `enforcement`, `recall`, `maude`, `gudid` | PROD (source of truth) |
| BigQuery `vigil_md_ops` | `load_log`, temporary `_tmp_*` tables | load bookkeeping |
| BigQuery `vigil_md_processed` | `*_flat`, `links_maude_gudid` | sample pipeline (unchanged) |

---

## GCS → BigQuery pipeline

Full design and decisions: [`docs/WAREHOUSE_PLAN.md`](docs/WAREHOUSE_PLAN.md).

### Three DAGs

| DAG | Schedule | What it does |
|---|---|---|
| `vigil_md_acquisition` | daily 06:00 UTC | Checks the FDA manifests. Fetches **only the sources that changed** to GCS (incremental). Validates GCS, logs a summary, then its last task `trigger_load` starts `vigil_md_load` with the same date. |
| `vigil_md_load` | none (triggered) | Runs **only after acquisition is done**. Per source: unzip → load RAW → build STAGING → MERGE into PROD. |
| `vigil_md_full_load` | none (manual) | First load, backfill, rebuild or recovery. Params: `sources`, `refetch`, `rebuild_prod`. |

```
vigil_md_acquisition (daily)                         vigil_md_load (triggered)
check_manifest ─┬─ fetch.<changed sources> ─ validate_gcs ─ log_summary ─┐
                ├─ (pending files only) ─────────────────────────────────┴─ trigger_load ──▶ load.<source>: unzip → load_raw → staging → merge
                └─ no_update (nothing to do: no loading run)
```

When does `vigil_md_load` run?

| Day | Loading run? |
|---|---|
| A source changed | Yes, after `log_summary` |
| No FDA change, but files from an earlier failed load are pending | Yes (retry) |
| No FDA change, nothing pending | No |
| Acquisition failed (fetch, validation or summary) | No |
| An acquisition run is cleared or retried | No second run (fixed `trigger_run_id`, `skip_when_already_exists`) |

### How the incremental rules work

| Rule | How |
|---|---|
| Incremental ingestion | Per partition (MAUDE quarter file, GUDID release), `records` + `size_mb` from `download.json` are compared with the saved state in `gs://vigil-md-raw-nd/manifests/partitions/{source}.json`. Unchanged → server-side copy in GCS, no download. |
| Content-hash check | Changed or new → download and SHA-256. Same hash as last load → no load, even if the export path is new. A second hash on the unzipped records catches a new zip with the same records. |
| Partition pruning | RAW and STAGING are partitioned by `_ingest_date` and **require a partition filter**. Staging reads one day of RAW. The MERGE limits PROD to the staging date range. |
| Idempotent loading | `vigil_md_ops.load_log` records every loaded file + content hash. The raw load deletes and inserts one file's rows for one day in a single transaction. Staging replaces one day. The MERGE updates a row only when `record_hash` changed. |

GUDID is an XML release (210 parts, about 5.2 million devices). It is converted to NDJSON **during the fetch** from a local temp copy, so it is never read back out of GCS. openFDA files are converted in the unzip step.

### GCS layout

| Path | What |
|---|---|
| `raw/{endpoint}/{export_date}/{quarter}/{file}.zip`, `raw/gudid/{snapshot}/{file}.zip` | Original downloads (source of record) |
| `ndjson/{source}/{date}/{partition_key}.ndjson.gz` | Gzip-compressed, one line per record: `{"payload": <record>}` |
| `manifests/manifest_state.json` | Export dates per source (read by `check_manifest`) |
| `manifests/partitions/{source}.json` | Per-partition fingerprint and hashes (incremental state) |
| `manifests/pending/{source}/*.json` | Files waiting to load. Deleted after the raw load commits. |
| `_incoming/` | Temporary, during a download |

### Going live (first time)

Follow the **Runbook** at the end of [`docs/WAREHOUSE_PLAN.md`](docs/WAREHOUSE_PLAN.md). Short version:

```bash
# 1. Pool: one BigQuery writer at a time (shared by vigil_md_load and vigil_md_full_load)
docker exec vigik-md-airflow-airflow-scheduler-1 airflow pools set vigil_warehouse 1 "BigQuery writers"

# 2. Tables (already created; safe to re-run)
docker exec -w /opt/airflow/Vigil-MD vigik-md-airflow-airflow-worker-1 \
  python scripts/warehouse/create_tables.py --project vigil-md-1792696816

# 3. Trigger vigil_md_full_load in the UI (try {"sources": ["recall"]} first),
#    keep both daily DAGs paused while it runs, then check row counts (Runbook step 4)

# 4. Unpause the loading DAG first, then acquisition
docker exec vigik-md-airflow-airflow-scheduler-1 airflow dags unpause vigil_md_load
docker exec vigik-md-airflow-airflow-scheduler-1 airflow dags unpause vigil_md_acquisition
```

Why this order: the incremental fetch **stops with an error** until a full load has written `manifests/partitions/*.json`.

### Running a warehouse step by hand

Every warehouse script has `--print-only`: it prints the plan and SQL and makes **no GCP call**. Run scripts inside the Airflow worker (it has the GCP libraries and credentials):

```bash
W="docker exec -w /opt/airflow/Vigil-MD vigik-md-airflow-airflow-worker-1 python"
$W scripts/warehouse/unzip_to_ndjson.py --bucket vigil-md-raw-nd --source maude --scope changed --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/load_raw_to_bq.py  --project vigil-md-1792696816 --bucket vigil-md-raw-nd --source maude --scope changed --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/build_staging.py   --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/merge_prod.py      --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25 --print-only
```

To load one day by hand, trigger `vigil_md_load` with `{"ingest_date": "YYYY-MM-DD"}`. To rebuild PROD for a source, trigger `vigil_md_full_load` with `{"sources": ["maude"], "rebuild_prod": true}` (`merge_prod.py --rebuild` refuses to run without `--yes-rebuild`).

---

## BigQuery tables

Defined in one place: [`scripts/warehouse/schemas.py`](scripts/warehouse/schemas.py). `create_tables.py` creates them and **raises if an existing table has different columns**. It never touches `vigil_md_processed.*` or `*_sample` tables.

### Layers

| Table | Partition | Cluster | Key | Contents |
|---|---|---|---|---|
| `vigil_md_raw.{source}_raw` | `_ingest_date` (DAY), filter required | `_source_file` | — | One row per source record, unchanged, in `payload` (JSON). Plus `_source_file`, `_source_partition`, `_zip_sha256`, `_content_sha256`, `_loaded_at`. |
| `vigil_md_staging.{source}_stg` | `_ingest_date` (DAY), filter required | merge key | merge key | Flattened and typed columns, deduplicated per day, `record_hash`. |
| `vigil_md_prod.{enforcement,recall,maude}` | `_part_date` (MONTH) = `COALESCE(record_date, 1900-01-01)` | merge key | merge key | Upserted source of truth. `first_loaded_at`, `last_updated_at`. |
| `vigil_md_prod.gudid` | none (its dates change) | `source_record_id` | `source_record_id` | Same as above. |
| `vigil_md_ops.load_log` | none | `source` | — | One row per loaded file: `source_file`, `zip_sha256`, `content_sha256`, `ingest_date`, `row_count`, `mode`, `loaded_at`. |

`{source}` is `enforcement`, `recall`, `maude` or `gudid`.

### Merge keys

| Source | Merge key | Notes |
|---|---|---|
| enforcement | `source_record_id` (= `recall_number`) | |
| recall | `source_record_id` (= `product_res_number`) | |
| maude | `source_record_id` (= `mdr_report_key`) + `device_seq` | One row per device in `device[]`. A report with no devices keeps one row (`device_seq` 0). |
| gudid | `source_record_id` (= `publicDeviceRecordKey`) | `udi_di` = the Primary `deviceId`. |

Rows with a NULL merge key are not staged (a warning with the count is logged). RAW keeps them.

### Columns (staging and prod)

Same names as `flatten_to_parquet.py`. `record_date` / `publish_date` are DATE. Full lists: `schemas.FLAT_COLUMNS`.

| Source | Columns |
|---|---|
| enforcement | `source`, `source_record_id`, `link_key`, `event_id`, `manufacturer_name_raw`, `manufacturer_name_norm`, `device_name_raw`, `recall_class`, `reason_text`, `scope_text`, `record_date`, `status`, `distribution_pattern` |
| recall | `source`, `source_record_id`, `link_key`, `res_event_number`, `manufacturer_name_raw`, `manufacturer_name_norm`, `device_name_raw`, `product_code`, `reason_text`, `scope_text`, `record_date`, `recall_status`, `root_cause`, `action` |
| maude | `source`, `source_record_id`, `device_seq`, `manufacturer_name_raw`, `manufacturer_name_norm`, `device_name_raw`, `model_number_raw`, `catalog_number`, `lot_number`, `udi_di`, `product_code`, `record_date`, `event_type`, `adverse_event_flag`, `report_number` |
| gudid | `source`, `source_record_id`, `udi_di`, `manufacturer_name_raw`, `manufacturer_name_norm`, `device_name_raw`, `model_number_raw`, `catalog_number`, `product_codes` (ARRAY), `device_record_status`, `publish_date`, `distribution_status` |

### Querying

RAW and STAGING **reject queries without a filter on `_ingest_date`** (this keeps scans small):

```sql
-- PROD: the source of truth (no filter needed; filter on _part_date to scan less)
SELECT source_record_id, manufacturer_name_norm, recall_class, record_date
FROM `vigil-md-1792696816.vigil_md_prod.enforcement`
WHERE _part_date >= '2024-01-01';

-- RAW: the original record for one day
SELECT JSON_VALUE(payload, '$.recall_number') AS recall_number, payload
FROM `vigil-md-1792696816.vigil_md_raw.enforcement_raw`
WHERE _ingest_date = '2026-10-26'
LIMIT 10;

-- All history (any filter on the partition column works)
SELECT COUNT(*) FROM `vigil-md-1792696816.vigil_md_staging.maude_stg`
WHERE _ingest_date >= '1900-01-01';

-- What was loaded, per source
SELECT source, COUNT(*) AS files, SUM(row_count) AS rows, MAX(loaded_at) AS last_load
FROM `vigil-md-1792696816.vigil_md_ops.load_log`
GROUP BY source;
```

---

## Bias slice analysis

`scripts/analysis/bias_slices.py` checks whether linking and data quality are uneven across slices of the data. It does not change the pipeline:

- It runs **read-only `SELECT`s on the four PROD tables only**.
  - No RAW JSON is scanned and no table is created.
  - The matching rules are the ones in `exact_linking.py`, unchanged.
  - A guard in the script rejects any other dataset or any write statement.
- Slices use **PROD columns only**: year, event type, adverse-event flag, product code, manufacturer, manufacturer volume tier, recall class/status/root cause, and GUDID status.
  - Slices that exist only in RAW (device class, specialty, country, patient sex/age, reporter type) are listed as limitations.

### What it measures

| Metric | Definition |
|---|---|
| UDI availability | MAUDE device rows with a `udi_di` |
| Primary-DI match | Of those rows, the share whose `udi_di` exactly equals a GUDID primary DI |
| End-to-end link coverage | MAUDE→GUDID (availability × match), and recall↔enforcement on `link_key` |
| Field completeness | Non-null, non-blank rate of every PROD business column |

- Each slice gets a Wilson 95% CI and its gap to the overall rate.
- A slice is flagged when n ≥ 30, the gap is at least 10 points, and its CI excludes the overall rate.
- `unmatched_udi_shape.csv` is a diagnostic only. It explains why unmatched UDIs don't match.

### Results (2026-10-26)

- **MAUDE→GUDID:** 61.9% of MAUDE rows have a UDI, 81.2% of those match, so **50.2%** link end-to-end.
- **Who links less:** Death reports, small manufacturers and 2016–17 reports. They are under-linked because the UDI is missing.
- **Who has a UDI but doesn't match:** several diabetes-device makers.
- **Formatting is not the cause:** normalization would recover only 20 of 2.5M unmatched UDIs.
- **Recall→enforcement (67.7%):** mostly a date-range effect. Recalls before 2012 have no enforcement record.

`scripts/analysis/bias_mitigation.py` then mitigates the main finding (linked MAUDE rows are not representative) by reweighting the linked rows on event type × manufacturer tier × year group, checks the result on variables it did not use, and reports the cost as the effective sample size. Fairlearn `MetricFrame` computes the link rate per slice and is cross-checked against `bias_slices.py`. Results are in `reports/bias/`; the recommended weights are `weight_full_trimmed` in `reports/bias/mitigation_weights.csv`.

Full results, limitations, the implemented mitigation (section 6) and further proposed mitigations (section 7) are in [`docs/BIAS_REPORT.md`](docs/BIAS_REPORT.md).

### How to run it

Run it from WSL with the `vigil-analysis` venv, which has `google-cloud-bigquery`. It uses your ADC credentials; if they've expired, run `gcloud auth application-default login`.

```bash
cd ~/Vigik-MD-airflow
PY=~/.venvs/vigil-analysis/bin/python

# 1. Print the SQL (no GCP call)
$PY scripts/analysis/bias_slices.py --project vigil-md-1792696816 --print-only

# 2. Free dry-run cost estimates (nothing is executed)
$PY scripts/analysis/bias_slices.py --project vigil-md-1792696816 --estimate-only --max-gb 5

# 3. Real run → data/interim/bias/
$PY scripts/analysis/bias_slices.py --project vigil-md-1792696816 --max-gb 5

# 4. Mitigation (reweighting + Fairlearn) → reports/bias/
$PY scripts/analysis/bias_mitigation.py --project vigil-md-1792696816
```

- **Cost:** the whole run scans about **7.6 GB**. A re-run within 24 hours can come from the BigQuery result cache and scan nothing.
- **`--max-gb 5` is needed** because the MAUDE completeness query scans about 3.9 GB, above the default 2 GB.
- **What `--max-gb` does:** it is checked against each query's dry run before anything runs, and it is also set as `maximum_bytes_billed` on every real query.
- **Exit codes:** `0` = done, `3` = stopped by the cost guard. A query that returns zero rows fails loudly.
- **Other output folder:** use `--out <dir>`.

**Output** in `data/interim/bias/` (git-ignored):

| File | Contents |
|---|---|
| `udi_availability.csv` | UDI availability (and length-rule violations) per slice |
| `primary_di_match.csv` | Primary-DI match rate per slice |
| `link_coverage.csv` | MAUDE→GUDID, recall→enforcement and enforcement→recall per slice |
| `field_completeness.csv` | Completeness per table, field and slice |
| `unmatched_udi_shape.csv` | Why unmatched UDIs did not match (diagnostic) |
| `summary.json` | Overall rates, disparity flags per metric, best/worst slices, data snapshot, bytes scanned, limitations |

Every CSV except `unmatched_udi_shape.csv` has the columns `metric, table, field, slice_name, slice_value, n, numerator, rate, ci_low, ci_high, overall_rate, gap_pp, ratio_to_overall, small_cell, below_four_fifths, disparity_flag`.

`docs/BIAS_REPORT.md` is written by hand from these files. After a re-run, compare `summary.json` with the report and update the report's numbers.

---

## Branch Guide

| Branch | Purpose |
|---|---|
| `main` | Stable code — all scripts |
| `airflow-integration` | All scripts + Docker + DAGs + the GCS → BigQuery warehouse |
| `airflow-setup` | Teammate's original Docker setup |
| `sample-data-pull` | Original sample pull work |

---

## Setup

**Requirements:** Python 3.10+ (3.12 recommended; needed by Great Expectations), Docker, Git, Google Cloud SDK

```bash
# Clone and set up environment
git clone https://github.com/NainicaD/Vigil-MD.git
cd Vigil-MD
git checkout airflow-integration

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install "dvc[gs]"
```

**GCP authentication:**

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project vigil-md-1792696816
gcloud auth application-default set-quota-project vigil-md-1792696816
```

The Airflow containers mount `~/.config/gcloud` read-only. They need `~/.config/gcloud/application_default_credentials.json`, which `gcloud auth application-default login` creates.

**Pull versioned data with DVC:**

```bash
dvc pull
```

This downloads all sample JSON files and processed Parquet files from GCS. No need to re-run acquisition scripts.

---

## Running the Pipeline

### Option A — Manual (step by step, sample data)

```bash
# 1. Pull 200 sample records per source
python scripts/acquisition/pull_samples.py --limit 200

# 2. Flatten and normalize
python scripts/preprocessing/flatten_to_parquet.py

# 3. Exact linking
python scripts/matching/exact_linking.py

# 4. Upload samples to GCS
python scripts/acquisition/upload_samples_to_gcs.py --bucket vigil-md-raw-nd

# 5. Load to BigQuery
python scripts/acquisition/load_interim_to_bq.py \
    --project vigil-md-1792696816 \
    --bucket vigil-md-raw-nd
```

### Option B — Full data pull to GCS (2016–2026)

```bash
python scripts/acquisition/fetch_sources.py \
    --bucket vigil-md-raw-nd \
    --start-year 2016 \
    --end-year 2026 \
    --mode full
```

Streams all FDA sources directly to GCS. Re-running is safe — files already in GCS are skipped automatically. Use `--dry-run` to preview what will be downloaded without downloading anything.

`--mode incremental` is the default (used by the daily DAG). It **raises** until a full load has written the partition state, so use `--mode full` for a first pull.

### Option C — GCS → BigQuery

Use the DAGs (see "GCS → BigQuery pipeline"). For one step by hand, see "Running a warehouse step by hand".

---

## Running Airflow with Docker

```bash
# 1. Create your .env
cp .env.example .env

# 2. Initialize the database (first time only)
docker compose up airflow-init

# 3. Start all services
docker compose up -d

# 4. Open the UI
open http://localhost:8080
# Login: airflow / airflow

# 5. Create the warehouse pool (first time only)
docker exec vigik-md-airflow-airflow-scheduler-1 airflow pools set vigil_warehouse 1 "BigQuery writers"
```

## Running ELK Stack (Logging)

ELK (Elasticsearch, Logstash, Kibana) runs separately from Airflow.
Start it when you want to search and visualize pipeline logs.

**Start ELK:**
```bash
docker compose -f docker-compose.elk.yml up -d
```

**Open Kibana:** http://localhost:5601

**What it collects:**
- All Airflow task logs (including historical logs from previous runs)
- Structured logs from Python scripts (fetch_sources.py, flatten_to_parquet.py, etc.)

**Search logs in Kibana:**
1. Go to http://localhost:5601
2. Click Discover in the left sidebar
3. Create index pattern: `vigil-md-logs-*`
4. Search by field: `dag_id`, `task_id`, `source`, `log_level`

**Stop ELK:**
```bash
docker compose -f docker-compose.elk.yml down
```

**Note:** Historical logs are preserved in the `logs/` folder and will be
indexed automatically when ELK starts, even if it wasn't running during
those pipeline runs. Elasticsearch data persists in a Docker volume
(`elasticsearch-data`) across restarts.

**DAGs:**

| DAG | Purpose | When to use |
|---|---|---|
| `pull_samples` | Pulls 200 sample records per source | Development and testing |
| `vigil_md_acquisition` | Daily incremental pull to GCS with validation; starts `vigil_md_load` | Production |
| `vigil_md_load` | GCS → RAW → STAGING → PROD | Production (started by acquisition) |
| `vigil_md_full_load` | First load, backfill, rebuild | Manual |

**Everyday commands:**

```bash
docker compose ps                          # service status
docker compose logs -f airflow-scheduler   # follow scheduler logs
docker compose logs airflow-dag-processor  # DAG import errors
docker compose down                        # stop, keep database
docker compose down -v                     # stop and wipe database
```

**Troubleshooting:**

| Problem | Fix |
|---|---|
| Port 8080 in use | Change `8080:8080` mapping in `docker-compose.yaml` |
| Task fails writing to `data/` | Set `AIRFLOW_UID` in `.env` to your `id -u`, restart |
| DAG missing from UI | Check `docker compose logs airflow-dag-processor`. New DAG files are picked up within 5 minutes. |
| `import requests` fails in task | Add `requests` to `_PIP_ADDITIONAL_REQUIREMENTS` in `.env` |
| Warehouse tasks never start | Create the `vigil_warehouse` pool (see above) |
| `No partition state for <source>. Run vigil_md_full_load first.` | Run the full load once (Runbook) |
| GCS calls fail with an auth error in tasks | Run `gcloud auth application-default login` on the host |

---

### After pulling the Airflow 3 log-parsing fix

Airflow 3 writes task logs as one JSON object per line. Filebeat now sends each line as its own
event (no multiline joining), and Logstash parses it with a `json` filter. Searchable fields include
`log_level`, `log_message`, `task_id`, `attempt`, `error_type` and `error_message`, and `@timestamp`
is the time the event happened (not the ingest time).

ELK runs on each machine separately, so after pulling this change, apply it locally:

```bash
# 1. Load the new Logstash pipeline
docker compose -f docker-compose.elk.yml restart logstash

# 2. Remove Filebeat (this also resets its record of which log files it has read)
docker compose -f docker-compose.elk.yml rm -sf filebeat

# 3. Delete the Airflow log entries indexed with the old parsing
#    (keeps the logs Python scripts sent to Logstash directly)
curl -s -X POST "localhost:9200/vigil-md-logs-*/_delete_by_query?refresh=true" \
  -H "Content-Type: application/json" \
  -d '{"query": {"term": {"tags.keyword": "airflow"}}}'

# 4. Start Filebeat again: it re-reads every file in logs/ with the new parsing
docker compose -f docker-compose.elk.yml up -d filebeat
```

Then refresh the `vigil-md-logs-*` data view in Kibana (Stack Management -> Data Views) so the new
fields appear. A search for `tags: "_jsonparsefailure" or tags: "_grok_parse_failure"` should return
no results.

**Day to day, stop ELK with `stop` rather than `down`:** `down` deletes the Filebeat container, so the
next `up` re-reads every log file and indexes it a second time. `stop` keeps it.

```bash
docker compose -f docker-compose.elk.yml stop    # pause
docker compose -f docker-compose.elk.yml start   # resume
```

## Data Versioning with DVC

DVC versions the sample data and the local sample pipeline. The data lives in the DVC remote
`gs://vigil-md-raw-nd/dvc-store`; git tracks only the pointer files (`data/raw/samples.dvc`,
`dvc.yaml`, `dvc.lock`).

The full-scale data (Airflow -> GCS -> BigQuery) is not managed by DVC. It is versioned by the
unchanged source zips in `raw/`, per-file content hashes, the pending manifests, and
`vigil_md_ops.load_log`.

### Get the data

```bash
dvc pull
```

### Sample pipeline (`dvc.yaml`)

```
data/raw/samples.dvc
        |
     flatten
     /     \
exact_linking  blocking
```

| Stage | Script | Outputs in `data/interim/` |
|---|---|---|
| `flatten` | `scripts/preprocessing/flatten_to_parquet.py` | `{enforcement,recall,maude,gudid}_flat.parquet` |
| `exact_linking` | `scripts/matching/exact_linking.py` | `links_enforcement_recall.parquet`, `links_maude_gudid.parquet`, `linking_report.json` |
| `blocking` | `scripts/matching/blocking.py` | `candidate_pairs.parquet`, `blocking_report.json` |

`dvc.lock` records the hash of every script, input and output, so a run can be reproduced exactly.
The matching scripts always write their outputs, as empty tables when nothing matches, so every
run produces the same set of files.

### Reproduce

```bash
dvc repro     # re-runs only the stages whose script or inputs changed
dvc status    # 'Data and pipelines are up to date' when outputs match dvc.lock
dvc dag       # shows the stage graph
```

### After changing the samples or a pipeline script

```bash
dvc add data/raw/samples          # only if the sample files changed
dvc repro                         # rebuild the affected stages
dvc push                          # upload the new data versions
git add data/raw/samples.dvc dvc.lock
git commit -m "data: <what changed>"
git push
```

Do not run `dvc add data/interim`: those files are produced by the pipeline and tracked in `dvc.lock`.

---

## Data Validation (Schema and Statistics)

`scripts/validation/` checks each staging table's schema and values and generates per-column
statistics, one load (`_ingest_date` partition) at a time. It only reads BigQuery.

| Check | How | Source of the rules |
|---|---|---|
| Schema: columns, types, modes | Live table metadata vs `schemas.py` (no data read, no cost) | Generated from `scripts/warehouse/schemas.py` |
| Keys not NULL and unique | Great Expectations | Generated from `MERGE_KEYS` |
| Allowed values, formats, date ranges | Great Expectations, with `mostly` margins | `RULES` in `scripts/validation/expectations.py` |
| Statistics per column | One query per table | Null %, distinct, blanks, text lengths, top values, min/max |

Comparisons with earlier loads (null-rate changes, volume, category mix) and alerts belong to the
anomaly checks, not to this module.

### Run

```bash
# Schema check only (free)
python scripts/validation/run_validation.py --project vigil-md-1792696816

# Cost estimate (BigQuery dry run, free) for the value checks and statistics
python scripts/validation/run_validation.py --project vigil-md-1792696816 --values --stats --estimate-only

# Value checks and statistics for one source (latest load by default)
python scripts/validation/run_validation.py --project vigil-md-1792696816 \
  --source recall --values --stats
```

Options: `--ingest-date YYYY-MM-DD` picks a load; `--max-gb` (default 2) stops any query estimated
above it; `--out-dir` sets where statistics are written. Exit codes: `0` all passed, `3` a check
failed (a data failure, not worth retrying), anything else an unexpected error.

### Outputs and cost

Statistics are written to `reports/statistics/<source>/<ingest_date>.json`, one file per load, so
loads can be compared over time. The first set (2026-10-26) is committed as the baseline.

Value-check results (each expectation, its rule, pass or fail, and the count of unexpected
rows) are written to `reports/validation/<source>/<ingest_date>.json` in the same layout.

The value checks run several queries per source (Great Expectations computes around 40-50
metrics), so a run can cost up to about 40x the per-query estimate; the statistics are one query
per table. On the full load (2026-10-26) the statistics cost about 6 GB for all four sources.
Daily loads only contain changed files, so their partitions, and the cost, are much smaller.

### Results (2026-10-26 full load)

All four schemas match, and the value checks pass: enforcement 5/5, recall 5/5, GUDID 5/5,
MAUDE 6/6. The GUDID UDI-DI length rule first failed on 312 six-character identifiers outside
the 7-23 range taken from a top-20 baseline; all 312 are HIBCC identifiers, so the rule now
allows 6-23.

### In the pipeline

Both warehouse DAGs (`vigil_md_load`, daily, and `vigil_md_full_load`, manual) run two check
steps for each source, after staging and before the merge into PROD:

```
unzip -> load_raw -> staging -> validate -> check -> merge
```

- `validate`: `run_validation.py --values --stats` (schema check, Great Expectations and per-column
  statistics). Results go to `logs/validation/<source>/<ingest_date>.json` and statistics to
  `logs/statistics/<source>/<ingest_date>.json`. A partition with no rows saves no statistics file,
  and statistics above 5 GB are skipped with a warning (a cost limit is not a data failure).
- `check`: `scripts/warehouse/check_staging.py` (anomaly checks: raw-to-staged gap, volume,
  null rates, dates, allowed values, category mix).

Exit code `3` means a check found a data problem. The task then fails without retrying
(`DataCheckFailed`), because the data would be the same; other errors are retried as usual.

`CHECKS_ENFORCE` in `dags/vigil_checks.py` is one switch for both DAGs:

| Value | What a failed check does |
|---|---|
| `False` (current) | Logs a warning; the merge continues |
| `True` | Fails the task: that source is not merged and the alert e-mail is sent |

Keep `False` until several daily loads have passed without false alarms, then set `True`.

The Airflow containers need Great Expectations: set `_PIP_ADDITIONAL_REQUIREMENTS` in `.env`
as in `.env.example`, then run `docker compose up -d`.

---

## Testing

**CI:** `.github/workflows/tests.yml` runs `pytest` on GitHub Actions (Python 3.12) for every
pull request and every push to `main`. It has no GCP credentials, so the integration tests are
excluded (as locally), and the data-contract and DAG tests skip; the log lists each skip reason.

> **DAG tests** (`tests/dags`) skip where Airflow is not installed. Run them in the worker:
> `docker compose exec -w /opt/airflow/Vigil-MD airflow-worker python -m pytest tests/dags -q`.
> They use a temporary SQLite database and refuse to reset any other. Older versions of
> `tests/dags/conftest.py` could reset the real Airflow database (run history, pools, pause
> settings) when run inside a container.

All GCP calls are mocked: **no test touches real GCS or BigQuery**. Test logs never reach Logstash (an autouse fixture). The case list is in [`tests/TEST_CASES.md`](tests/TEST_CASES.md).

```bash
# Everything except the two pandas-based files
~/.venvs/vigil-airflow/bin/python -m pytest tests \
    --ignore=tests/test_data_quality.py --ignore=tests/test_preprocessing.py
# 2026-10-26: 344 passed, 55 skipped, 21 deselected (integration), 4 failed
#   (test_unzip_to_ndjson.py: google-cloud-storage is not installed in vigil-airflow)

# Bias slice analysis only (FakeClient, no GCP)
~/.venvs/vigil-airflow/bin/python -m pytest tests/test_bias_slices.py

# Real-GCS checks of the raw files (needs credentials; run by the validate_gcs task)
~/.venvs/vigil-airflow/bin/python -m pytest tests -m integration
```

The warehouse conversion tests need `ijson` (in `requirements.txt`). The WSL venvs are made with `uv` and have no `pip`: `~/.local/bin/uv pip install --python ~/.venvs/vigil-airflow/bin/python ijson`.

The BigQuery SQL is checked by hand with free dry runs. See "Manual checks" in `tests/TEST_CASES.md`.

---

## Known limitations and issues

- **MAUDE scope is 2016–2026 by design** (`START_YEAR` / `END_YEAR` in the DAGs). Quarters from 2027 on are not fetched. To extend it, change `END_YEAR` in `vigil_md_acquisition.py` and `vigil_md_full_load.py`.
- Open issues are tracked in [`docs/issue.md`](docs/issue.md). Examples: the parallel fetch tasks can overwrite `manifest_state.json` keys (#5), task logs appear only when a task ends (#8), and the manufacturer suffix rule turns `"Tesco"` into `"tes"` (#13).

---

## Changes

### 2026-10-26 — Bias slice analysis of PROD

- **New `scripts/analysis/bias_slices.py`:**
  - Measures UDI availability, primary-DI match rate, end-to-end link coverage and field completeness, broken down by PROD-column slices.
  - Runs read-only SELECTs on `vigil_md_prod` only, with a dry-run cost guard and `maximum_bytes_billed`.
- **Results:** CSVs and `summary.json` in `data/interim/bias/` (git-ignored). The report is [`docs/BIAS_REPORT.md`](docs/BIAS_REPORT.md).
- **Tests:** 37 mocked tests in `tests/test_bias_slices.py`, listed in `tests/TEST_CASES.md` §19.
- **Unchanged:** the pipeline, the matching rules and every warehouse table.

### 2026-10-25 — GCS → BigQuery warehouse (RAW → STAGING → PROD)

- **New BigQuery layers:** `vigil_md_raw.*_raw`, `vigil_md_staging.*_stg`, `vigil_md_prod.*`, `vigil_md_ops.load_log` (`scripts/warehouse/`). Existing `*_sample` and `vigil_md_processed.*` tables are unchanged.
- **Incremental fetch:** `fetch_sources.py --mode incremental` (new default) with per-partition fingerprint and SHA-256 checks, pending manifests, and a server-side copy for unchanged files. `--mode full` keeps the old behaviour.
- **GUDID:** the bulk release is XML, not JSON. It is converted to NDJSON during the fetch (`gudid_convert.py`), from a local copy, with part and record counts checked against the XML header.
- **DAGs:** `vigil_md_acquisition` now fetches only changed sources and starts the new `vigil_md_load` when it is done. New manual `vigil_md_full_load`. All BigQuery writers share the 1-slot pool `vigil_warehouse`.
- **Tests:** 238 new mocked tests (`tests/test_*` for the warehouse, `tests/dags/test_warehouse_dags.py`), shared in-memory GCS / HTTP fakes, and a Logstash guard in `tests/conftest.py`.
- **Docs:** `docs/WAREHOUSE_PLAN.md` (design + Runbook), `docs/TESTS_PLAN.md`, `docs/issue.md`. `docs/` is no longer ignored by git.
- **Fixed during the work:** a failed conversion could leave a partial NDJSON file in GCS (`docs/issue.md` #12); the GUDID MERGE bounds query used an untyped NULL.

---

## Roadmap

- [x] Sample data pull (all 4 sources)
- [x] Preprocessing and normalization
- [x] Exact linking — 108 MAUDE–GUDID links confirmed
- [x] GCS and BigQuery integration
- [x] Full bulk download (2016–2026)
- [x] Airflow DAG with Docker
- [x] DVC data versioning
- [x] Test modules
- [x] Incremental RAW → STAGING → PROD warehouse in BigQuery (go-live: Runbook)
- [x] Slice analysis: coverage and bias across data slices (`docs/BIAS_REPORT.md`)
- [ ] Bias mitigations (proposed in `docs/BIAS_REPORT.md` §6)
- [ ] Candidate generation and ML matching
- [ ] Unified BigQuery database with provenance
- [ ] Chat interface for recall history search

---

## Disclaimer

Vigil MD is a course project for research and educational purposes. It is not a medical tool. FDA data from openFDA is unvalidated, and adverse event reports alone do not establish that a device caused an outcome. Always consult a clinician and the official FDA source for decisions about medical devices.
