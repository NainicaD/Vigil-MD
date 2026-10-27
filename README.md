# Vigil MD

**ML-based entity resolution across FDA medical device databases.**

Vigil MD links medical device records from four FDA sources into one evidence-linked database. It finds records in different sources that describe the same manufacturer, device model or recall, and keeps genuinely different devices and recalls apart. The goal is a reusable linkage layer, so researchers can search a device's or manufacturer's recall history without rebuilding the links.

Inspired by Kramer et al. (2012), *Security and Privacy Qualities of Medical Devices: An Analysis of FDA Postmarket Surveillance*, PLoS ONE.

**Status (2026-10-27).** The full 2016–2026 data is in a BigQuery warehouse (RAW → STAGING → PROD, 26.9 million PROD rows). Three Airflow DAGs fetch, load, validate and merge it daily. Each load passes schema and value checks, per-column statistics and anomaly checks before it reaches PROD; a failed check stops that source's merge and sends an e-mail alert. A bias analysis of PROD, with a reweighting mitigation, is in [`docs/BIAS_REPORT.md`](docs/BIAS_REPORT.md). 460 tests run in CI for every pull request.

**Contents:** [Quickstart](#quickstart) · [How the pipeline works](#how-the-pipeline-works) · [Data checks and alerts](#data-checks-and-alerts) · [Scheduling](#scheduling-pools-and-priorities) · [BigQuery tables](#bigquery-tables) · [Data validation](#data-validation-schema-and-statistics) · [Anomaly detection](#anomaly-detection) · [Bias analysis](#bias-analysis-and-mitigation) · [DVC](#data-versioning-with-dvc) · [Logging (ELK)](#logging-elk) · [Testing and CI](#testing-and-ci) · [Repository structure](#repository-structure) · [Troubleshooting](#everyday-commands-and-troubleshooting) · [Changes](#changes)

---

## Data sources

| Source | URL | Role | Updates |
|---|---|---|---|
| FDA Enforcement | https://open.fda.gov/apis/device/enforcement/ | Recall actions and classification | Weekly |
| Device Recall | https://open.fda.gov/apis/device/recall/ | Recall details and corrective actions | Weekly |
| MAUDE | https://open.fda.gov/apis/device/event/ | Adverse event and malfunction reports | Quarterly files |
| GUDID | https://accessgudid.nlm.nih.gov/download | Canonical device identifiers | Monthly |

Scope: all device types; MAUDE 2016–2026. Enforcement, recall and GUDID are loaded in full. The only reliable common key is the UDI device identifier (UDI-DI): every GUDID record has one, and only part of MAUDE does.

---

## Quickstart

From a clean clone to a first pipeline run. Tested on macOS with Docker Desktop and Python 3.12. Each step says what you should see.

**Prerequisites:** Docker Desktop (at least 4 GB of memory for Docker), Python 3.10+ (3.12 tested; Great Expectations needs 3.10+), Git, the Google Cloud SDK, and access to the GCP project `vigil-md-1792696816` and the bucket `vigil-md-raw-nd`.

1. **Clone and install.**
   ```bash
   git clone https://github.com/NainicaD/Vigil-MD.git && cd Vigil-MD
   conda create -n vigil python=3.12 && conda activate vigil   # or: python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
   All direct dependencies are pinned (DVC included); the install should end without conflict errors.

2. **GCP credentials.**
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project vigil-md-1792696816
   gcloud auth application-default set-quota-project vigil-md-1792696816
   ```
   This creates `~/.config/gcloud/application_default_credentials.json`. The scripts, DVC and the Airflow containers all use it (the containers mount `~/.config/gcloud` read-only).

3. **Environment file.**
   ```bash
   cp .env.example .env
   ```
   Set `AIRFLOW_UID` to the output of `id -u`, and fill in the e-mail (SMTP) settings for the failure alerts. `_PIP_ADDITIONAL_REQUIREMENTS` is already set: it installs the Airflow providers, Great Expectations and sqlalchemy-bigquery in the containers.

4. **Start Airflow.**
   ```bash
   docker compose up airflow-init     # first time only: creates the metadata database and the admin user
   docker compose up -d
   docker compose ps
   ```
   The first start takes a few minutes (it installs the extra packages). Every `airflow-...` service should be `Up (healthy)`. The UI is at http://localhost:8080 (login `airflow` / `airflow`).

5. **Create the two pools (once per machine).** Without them, tasks stay queued with no error.
   ```bash
   docker compose exec airflow-scheduler airflow pools set vigil_warehouse 1 "BigQuery writers"
   docker compose exec airflow-scheduler airflow pools set vigil_upload 1 "GCS uploads (unzip)"
   docker compose exec airflow-scheduler airflow pools list
   ```
   You should see `vigil_warehouse` and `vigil_upload`, 1 slot each.

6. **Create the BigQuery tables** (safe to re-run; already done for the shared project).
   ```bash
   docker compose exec -w /opt/airflow/Vigil-MD airflow-worker \
     python scripts/warehouse/create_tables.py --project vigil-md-1792696816
   ```

7. **Run the tests.**
   ```bash
   pytest -q
   docker compose exec -w /opt/airflow/Vigil-MD airflow-worker python -m pytest tests/dags -q
   ```
   About 460 tests pass locally (the DAG tests skip there), and 68 DAG tests pass in the worker.

8. **Sample data (DVC).**
   ```bash
   dvc pull
   dvc repro
   dvc status
   ```
   `dvc status` should report `Data and pipelines are up to date`.

9. **First pipeline run: one small source.**
   ```bash
   docker compose exec airflow-scheduler airflow dags unpause vigil_md_full_load
   docker compose exec airflow-scheduler airflow dags trigger vigil_md_full_load \
     --conf '{"sources": ["enforcement"], "refetch": false, "rebuild_prod": false}'
   ```
   In the UI, enforcement's six steps (unzip, load_raw, staging, validate, check, merge) should turn green in about 3 minutes; the other sources are skipped. A full MAUDE load takes several hours, so start with enforcement. On an empty project, run the full load for all sources once before the daily DAGs (the incremental fetch needs the partition state it writes).

10. **Daily pipeline.** Unpause `vigil_md_load` first, then `vigil_md_acquisition`. Acquisition runs daily at 06:00 UTC, while the machine and Docker are running, and starts the load when it finishes.
    ```bash
    docker compose exec airflow-scheduler airflow dags unpause vigil_md_load
    docker compose exec airflow-scheduler airflow dags unpause vigil_md_acquisition
    ```

11. **Optional: logs in Kibana.** `docker compose -f docker-compose.elk.yml up -d`, then see [Logging (ELK)](#logging-elk).

To stop, use `docker compose stop` (not `down`), so nothing is recreated.

---

## How the pipeline works

Full design and decisions: [`docs/WAREHOUSE_PLAN.md`](docs/WAREHOUSE_PLAN.md).

### Three DAGs

| DAG | Schedule | What it does |
|---|---|---|
| `vigil_md_acquisition` | daily 06:00 UTC | Checks the FDA manifests and fetches **only the sources that changed** to GCS. Validates GCS, logs a summary, then `trigger_load` starts `vigil_md_load` with the same date. |
| `vigil_md_load` | none (triggered) | Per source: unzip → load RAW → build STAGING → **validate** → **check** → MERGE into PROD. A source with nothing pending does nothing. |
| `vigil_md_full_load` | none (manual) | First load, backfill, rebuild or recovery: the same six steps per source. Params: `sources`, `refetch`, `rebuild_prod`. Refuses to run while a daily DAG has a run in progress. |

```
vigil_md_acquisition (daily)                                   vigil_md_load (triggered)
check_manifest ─┬─ fetch.<changed sources> ─ validate_gcs ─ log_summary ─┐
                ├─ (pending files only) ─────────────────────────────────┴─ trigger_load ──▶ load.<source>:
                └─ no_update (nothing to do: no loading run)                  unzip → load_raw → staging → validate → check → merge
```

When does `vigil_md_load` run?

| Day | Loading run? |
|---|---|
| A source changed | Yes, after `log_summary` |
| No FDA change, but files from an earlier failed load are pending | Yes (retry) |
| No FDA change, nothing pending | No |
| Acquisition failed (fetch, validation or summary) | No |
| An acquisition run is cleared or retried | No second run (fixed `trigger_run_id`, `skip_when_already_exists`) |

Every DAG has `alert_on_failure` (trigger rule `ONE_FAILED`): it sends an e-mail when any task fails, including a failed data check.

### How the incremental rules work

| Rule | How |
|---|---|
| Incremental ingestion | Per partition (MAUDE quarter file, GUDID release), `records` + `size_mb` from `download.json` are compared with the saved state in `gs://vigil-md-raw-nd/manifests/partitions/{source}.json`. Unchanged → server-side copy in GCS, no download. |
| Content-hash check | Changed or new → download and SHA-256. Same hash as last load → no load, even if the export path is new. A second hash on the unzipped records catches a new zip with the same records. |
| Partition pruning | RAW and STAGING are partitioned by `_ingest_date` and **require a partition filter**. Staging reads one day of RAW. The MERGE limits PROD to the staging date range. |
| Idempotent loading | `vigil_md_ops.load_log` records every loaded file + content hash. The raw load deletes and inserts one file's rows for one day in a single transaction. Staging replaces one day. The MERGE updates a row only when `record_hash` changed. |

GUDID is an XML release (210 parts, about 5.2 million devices). It is converted to NDJSON **during the fetch** from a local temp copy, and each part's device count is checked against the XML header. openFDA files are converted in the unzip step, to gzip-compressed NDJSON.

### GCS layout

| Path | What |
|---|---|
| `raw/{endpoint}/{export_date}/{quarter}/{file}.zip`, `raw/gudid/{snapshot}/{file}.zip` | Original downloads (source of record) |
| `ndjson/{source}/{date}/{partition_key}.ndjson.gz` | Gzip-compressed, one line per record: `{"payload": <record>}` |
| `manifests/manifest_state.json` | Export dates per source (read by `check_manifest`) |
| `manifests/partitions/{source}.json` | Per-partition fingerprint and hashes (incremental state) |
| `manifests/pending/{source}/*.json` | Files waiting to load. Deleted after the raw load commits. |
| `_incoming/` | Temporary, during a download |

---

## Data checks and alerts

Between staging and the merge, each source runs two check steps in both warehouse DAGs:

| Step | Runs | What it checks |
|---|---|---|
| `validate` | `scripts/validation/run_validation.py --values --stats` | Live schema against `schemas.py`, Great Expectations value checks, and per-column statistics. See [Data validation](#data-validation-schema-and-statistics). |
| `check` | `scripts/warehouse/check_staging.py` | Anomaly checks against baselines and the previous load. See [Anomaly detection](#anomaly-detection). |

- Both exit with code `3` when they find a data problem. The DAG then raises `DataCheckFailed` (an `AirflowFailException`), so the task fails **without a retry**, because the data would be the same. Other errors (for example a network error) keep the normal retries.
- Results go to `logs/validation/<source>/<ingest_date>.json` and statistics to `logs/statistics/<source>/<ingest_date>.json` (outside the repository). A partition with no rows saves no statistics file, and statistics above 5 GB are skipped with a warning (a cost limit is not a data failure).
- `CHECKS_ENFORCE` in `dags/vigil_checks.py` is one switch for both DAGs:

| Value | What a failed check does |
|---|---|
| `True` (current, since 2026-10-27) | Fails the task: that source is not merged and the alert e-mail is sent; the other sources continue |
| `False` | Logs a warning; the merge continues (warn-only, for example while a threshold is tuned) |

Enforcement was switched on on 2026-10-27, after the checks passed on real loads (anomaly checks 18/18 for enforcement and 19/19 for recall; value checks on all four sources).

---

## Scheduling: pools and priorities

Each step runs in one of three pools (defined in `dags/vigil_checks.py`):

| Pool | Slots | Steps | Why |
|---|---|---|---|
| `vigil_upload` | 1 | unzip | GCS uploads share one connection |
| `vigil_warehouse` | 1 | load_raw, staging, merge, refetch | BigQuery cancels concurrent transactions on the same table (`load_log`) |
| `default_pool` | 128 | validate, check | Read-only queries |

The two one-slot pools run at the same time, so one source's BigQuery steps run while the next source uploads. All warehouse steps use `weight_rule="upstream"` with a priority per source (enforcement 4, recall 3, GUDID 2, MAUDE 1): small sources go first, and each source's chain finishes before the next source's long upload delays it.

Found and measured with Airflow Gantt charts (2026-10-27, full load of recall, enforcement and GUDID): before the change, enforcement's RAW load waited about 32 minutes for the single slot and every source finished at the end of the run; after it, enforcement was in PROD after about 50 seconds and recall after about 2 minutes. Details in the Data Pipeline Report, section 12.

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

## Data validation (schema and statistics)

`scripts/validation/` checks each staging table's schema and values with Great Expectations 1.24, and generates per-column statistics, one load (`_ingest_date` partition) at a time. It only reads BigQuery. In the pipeline it runs as the `validate` step; it can also run by hand.

| Check | How | Source of the rules |
|---|---|---|
| Schema: columns, types, modes | Live table metadata vs `schemas.py` (no data read, no cost) | `scripts/warehouse/schemas.py` |
| Keys not NULL and unique | Great Expectations | `MERGE_KEYS` |
| Allowed values, formats, date ranges | Great Expectations, with `mostly` margins | `RULES` in `scripts/validation/expectations.py` |
| Statistics per column | One query per table | Null %, distinct, blanks, text lengths, top values, min/max, array sizes |

Run by hand (needs the GCP credentials from the Quickstart):

```bash
# Schema check only (free)
python scripts/validation/run_validation.py --project vigil-md-1792696816

# Cost estimate (BigQuery dry run, free) for the value checks and statistics
python scripts/validation/run_validation.py --project vigil-md-1792696816 --values --stats --estimate-only

# Value checks and statistics for one source (latest load by default)
python scripts/validation/run_validation.py --project vigil-md-1792696816 --source recall --values --stats
```

Options: `--ingest-date YYYY-MM-DD` picks a load; `--max-gb` (default 2) stops any query estimated above it; `--stats-max-gb` sets a separate limit for the statistics; `--out-dir` and `--results-dir` set where statistics and results are written (by hand: `reports/statistics/` and `reports/validation/`; the first set, from 2026-10-26, is committed as the baseline). Exit codes: `0` all passed, `3` a check failed (a data failure, not worth retrying), anything else an unexpected error.

**Cost.** The value checks run several queries per source (Great Expectations computes about 40–50 metrics); the statistics are one query per table, about 6 GB for all four sources on a full load. Daily loads only contain changed files, so their partitions, and the cost, are much smaller.

**Results (2026-10-26 full load).** All four schemas match, and the value checks pass: enforcement 5/5, recall 5/5 (454 invalid product codes, inside the 2% margin), GUDID 5/5, MAUDE 6/6. The GUDID UDI-DI length rule first failed on 312 six-character identifiers; all 312 are HIBCC identifiers, so the rule now allows 6–23 characters.

---

## Anomaly detection

`scripts/warehouse/check_staging.py` checks one day of STAGING before it is merged into PROD. Thresholds and baselines are in `scripts/warehouse/check_config.py`.

| Check | Catches |
|---|---|
| Raw-to-staged gap | Rows lost in staging (also a load where every key is NULL, which would otherwise stage nothing with no error) |
| Volume | A drop against the previous load of the same source |
| Null rates | A rise above the baseline, per column |
| Dates | Dates in the future or before 1970 |
| Whitespace-only values, product codes, allowed values | Invalid formats and new categories |
| Category mix | A large shift in the share of a category |

Each check reports PASS, WARN or FAIL; a FAIL exits with code `3`. Options: `--project`, `--source`, `--ingest-date`, `--warn-only` (report FAILs but exit 0), `--print-only` (print the SQL, no GCP call).

---

## Bias analysis and mitigation

`scripts/analysis/bias_slices.py` checks whether linking and data quality are uneven across slices of the data; `scripts/analysis/bias_mitigation.py` corrects the main finding. Both run **read-only `SELECT`s on `vigil_md_prod` only** (a guard rejects any other dataset or any write), with a dry-run cost check and `maximum_bytes_billed` on every query. Full results, limitations and further proposals: [`docs/BIAS_REPORT.md`](docs/BIAS_REPORT.md).

**What it measures, per slice** (year, event type, adverse-event flag, product code, manufacturer, manufacturer tier, recall class/status/root cause, GUDID status): UDI availability, primary-DI match (the rule of `exact_linking.py`), end-to-end MAUDE→GUDID link coverage, recall↔enforcement link coverage, and field completeness. Each slice gets a Wilson 95% CI; a slice is flagged when n ≥ 30, the gap to the overall rate is at least 10 points, and its CI excludes the overall rate.

**Findings.** 61.9% of MAUDE rows have a UDI and 81.2% of those match, so 50.2% link end to end. The linked rows are skewed: Death reports are 0.55% of all rows but 0.42% of linked rows; top-10 manufacturers 52.3% and 58.4%; long-tail manufacturers 14.9% and 12.0%. Several diabetes-device makers have UDIs that do not match a primary DI; normalization would recover only 20 of 2.5 million unmatched UDIs.

**Mitigation.** Inverse probability weighting of the linked rows (cells: event type × manufacturer tier × year group), checked on two variables the weights do not use (adverse-event flag, product code), with the cost reported as the effective sample size (85.9% with the recommended trimmed weights). Fairlearn `MetricFrame` computes the link rate per slice and agrees with `bias_slices.py` within 5 × 10⁻⁷. The recommended weights are `weight_full_trimmed` in `reports/bias/mitigation_weights.csv`.

**Run it** (with the GCP credentials from the Quickstart):

```bash
python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --print-only               # SQL only, no GCP call
python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --estimate-only --max-gb 5 # free dry runs
python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --max-gb 5                 # → data/interim/bias/
python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816                         # → reports/bias/
```

- **Cost:** about 7.6 GB for `bias_slices.py` (`--max-gb 5` is needed: the MAUDE completeness query scans about 3.9 GB) and about 1.3 GB for the mitigation. A re-run within 24 hours can come from the BigQuery cache and scan nothing.
- **Order:** run `bias_slices.py` first; the mitigation's Fairlearn cross-check reads its `link_coverage.csv`. `bias_mitigation.py --from-cells data/interim/bias/mitigation_cells.csv` recomputes without querying BigQuery.
- **Exit codes:** `0` = done, `3` = stopped by the cost guard.
- **Outputs:** `data/interim/bias/` (git-ignored: `udi_availability.csv`, `primary_di_match.csv`, `link_coverage.csv`, `field_completeness.csv`, `unmatched_udi_shape.csv`, `summary.json`) and `reports/bias/` (committed: `mitigation_summary.json`, `mitigation_shares.csv`, `mitigation_weights.csv`).

`docs/BIAS_REPORT.md` is written by hand from these files. After a re-run, compare `summary.json` with the report and update its numbers.

---

## Data versioning with DVC

DVC versions the sample data and the local sample pipeline. The data lives in the DVC remote `gs://vigil-md-raw-nd/dvc-store`; git tracks only the pointer files (`data/raw/samples.dvc`, `dvc.yaml`, `dvc.lock`).

The full-scale data (Airflow → GCS → BigQuery) is not managed by DVC. It is versioned by the unchanged source zips in `raw/`, per-file content hashes, the pending manifests, and `vigil_md_ops.load_log`.

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

`dvc.lock` records the hash of every script, input and output, so a run can be reproduced exactly. The matching scripts always write their outputs, as empty tables when nothing matches, so every run produces the same set of files.

```bash
dvc pull      # get the data
dvc repro     # re-runs only the stages whose script or inputs changed
dvc status    # 'Data and pipelines are up to date' when outputs match dvc.lock
dvc dag       # shows the stage graph
```

After changing the samples or a pipeline script:

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

## Logging (ELK)

Every script logs through `scripts/logger.py` (console and Logstash). ELK (Elasticsearch, Logstash, Kibana) runs separately from Airflow:

```bash
docker compose -f docker-compose.elk.yml up -d      # first start
docker compose -f docker-compose.elk.yml stop       # pause (use stop, not down)
docker compose -f docker-compose.elk.yml start      # resume
```

Kibana is at http://localhost:5601. Filebeat reads all Airflow task logs in `logs/` (including logs from before ELK was started), and Logstash parses Airflow 3's one-JSON-object-per-line format. Searchable fields include `dag_id`, `task_id`, `attempt`, `log_level`, `log_message`, `error_type` and `error_message`; `@timestamp` is the time the event happened. In Kibana, open **Discover** and use the data view `vigil-md-logs-*`. Saved searches: *Full load runs* and *Errors*.

**Use `stop`, not `down`:** `down` deletes the Filebeat container, so the next `up` re-reads every log file and indexes it a second time.

**If your Kibana still has logs from before the Airflow 3 parsing fix** (2026-10-25), re-index them once:

```bash
docker compose -f docker-compose.elk.yml restart logstash
docker compose -f docker-compose.elk.yml rm -sf filebeat
curl -s -X POST "localhost:9200/vigil-md-logs-*/_delete_by_query?refresh=true" \
  -H "Content-Type: application/json" -d '{"query": {"term": {"tags.keyword": "airflow"}}}'
docker compose -f docker-compose.elk.yml up -d filebeat
```

Then refresh the `vigil-md-logs-*` data view (Stack Management → Data Views). A search for `tags: "_jsonparsefailure" or tags: "_grok_parse_failure"` should return nothing.

---

## Testing and CI

All GCP calls are mocked: **no test touches real GCS or BigQuery**, and test logs never reach Logstash (an autouse fixture). The case list is in [`tests/TEST_CASES.md`](tests/TEST_CASES.md); the test plan is [`docs/TESTS_PLAN.md`](docs/TESTS_PLAN.md).

```bash
pytest -q                                        # 460 passed, 1 skipped, 21 deselected (2026-10-27)
docker compose exec -w /opt/airflow/Vigil-MD airflow-worker python -m pytest tests/dags -q   # 68 passed
pytest -m integration                            # real-GCS checks of the raw files (needs credentials)
```

- **DAG tests** (`tests/dags`) skip where Airflow is not installed, so run them in the worker. They use a temporary SQLite database and refuse to reset any other. (Before 2026-10-27, `tests/dags/conftest.py` could reset the real Airflow database when run inside a container: run history, pools and pause settings.)
- **Integration tests** are excluded by default (`pytest.ini`: `-m "not integration"`).
- **CI:** `.github/workflows/tests.yml` runs `pytest` on GitHub Actions (Python 3.12) for every pull request and every push to `main`. It has no GCP credentials: the integration tests are excluded, and the data-contract and DAG tests skip; the log lists each skip reason.
- The BigQuery SQL is checked by hand with free dry runs (see "Manual checks" in `tests/TEST_CASES.md`).

---

## Repository structure

```
Vigil-MD/
├── dags/
│   ├── vigil_md_acquisition.py      # daily: FDA + GUDID → GCS, then starts vigil_md_load
│   ├── vigil_md_load.py             # triggered: GCS → RAW → STAGING → checks → PROD
│   ├── vigil_md_full_load.py        # manual: first load, backfill, rebuild
│   ├── vigil_checks.py              # shared: check rules, CHECKS_ENFORCE, pools, priorities
│   ├── pull_samples_dag.py          # development: 200 sample records per source
│   └── .airflowignore               # vigil_checks.py is not a DAG
├── scripts/
│   ├── acquisition/                 # fetch_sources.py (incremental | full), gudid_convert.py, pull_samples.py, sample upload/load
│   ├── warehouse/                   # schemas.py, create_tables.py, convert.py, common.py,
│   │                                #   unzip_to_ndjson.py, load_raw_to_bq.py, build_staging.py, merge_prod.py, sql/staging_*.sql,
│   │                                #   check_staging.py + check_config.py (anomaly checks)
│   ├── validation/                  # expectations.py, statistics.py, run_validation.py (schema, GX, statistics)
│   ├── preprocessing/               # schema_map.py, flatten_to_parquet.py (sample pipeline)
│   ├── matching/                    # exact_linking.py, blocking.py (sample pipeline)
│   ├── analysis/                    # bias_slices.py, bias_mitigation.py
│   └── logger.py                    # console + Logstash logger
├── tests/                           # pytest (GCP mocked); tests/dags = DAG tests; TEST_CASES.md
├── docs/                            # WAREHOUSE_PLAN.md (design + Runbook), TESTS_PLAN.md, BIAS_REPORT.md, issue.md
├── reports/                         # committed results: statistics/ and validation/ baselines, bias/ mitigation results
├── data/                            # DVC-tracked (not in git): raw/samples/, interim/
├── .github/workflows/tests.yml      # CI
├── dvc.yaml, dvc.lock               # DVC sample pipeline
├── docker-compose.yaml              # Airflow (CeleryExecutor, Postgres, Redis)
├── docker-compose.elk.yml           # Elasticsearch, Logstash, Kibana, Filebeat
├── .env.example                     # environment template (copy to .env)
├── requirements.txt                 # pinned direct dependencies
└── pytest.ini
```

### GCP resources

| Resource | Name | Used by |
|---|---|---|
| Project | `vigil-md-1792696816` | everything |
| GCS bucket | `vigil-md-raw-nd` | raw zips, NDJSON, manifests, DVC remote (`dvc-store/`) |
| BigQuery `vigil_md_raw` | `*_raw` tables (warehouse) and `*_sample` tables (samples) | RAW layer |
| BigQuery `vigil_md_staging` | `*_stg` tables | STAGING layer |
| BigQuery `vigil_md_prod` | `enforcement`, `recall`, `maude`, `gudid` | PROD (source of truth) |
| BigQuery `vigil_md_ops` | `load_log`, temporary `_tmp_*` tables | load bookkeeping |
| BigQuery `vigil_md_processed` | `*_flat`, `links_maude_gudid` | sample pipeline (legacy) |

---

## Everyday commands and troubleshooting

```bash
docker compose ps                          # service status
docker compose logs -f airflow-scheduler   # follow scheduler logs
docker compose logs airflow-dag-processor  # DAG import errors
docker compose exec airflow-scheduler airflow dags list-import-errors
docker compose stop                        # stop, keep containers and database
docker compose start                       # resume
```

Avoid `docker compose down -v`: it deletes the Airflow database (run history, pools).

| Problem | Fix |
|---|---|
| Port 8080 in use | Change the `8080:8080` mapping in `docker-compose.yaml` |
| Task fails writing to `data/` | Set `AIRFLOW_UID` in `.env` to your `id -u`, then `docker compose up -d` |
| DAG missing from the UI | `docker compose exec airflow-scheduler airflow dags list-import-errors`. New DAG files are picked up within a few minutes. |
| Warehouse tasks stay queued | Create both pools, `vigil_warehouse` and `vigil_upload` ([Quickstart](#quickstart) step 5) |
| `No module named 'great_expectations'` in `validate` | Set `_PIP_ADDITIONAL_REQUIREMENTS` in `.env` as in `.env.example`, then `docker compose up -d` |
| `No partition state for <source>. Run vigil_md_full_load first.` | Run the full load once for all sources |
| GCS or BigQuery calls fail with an auth error | Run `gcloud auth application-default login` on the host |
| A `validate` or `check` task failed | A data check found a problem (exit 3): read the task log for the FAIL lines, fix the data or the threshold, then clear the task |

### Running steps by hand

Every warehouse script has `--print-only`: it prints the plan and SQL and makes **no GCP call**. Run scripts inside the Airflow worker (it has the GCP libraries and credentials):

```bash
W="docker compose exec -w /opt/airflow/Vigil-MD airflow-worker python"
$W scripts/warehouse/unzip_to_ndjson.py --bucket vigil-md-raw-nd --source maude --scope changed --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/load_raw_to_bq.py  --project vigil-md-1792696816 --bucket vigil-md-raw-nd --source maude --scope changed --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/build_staging.py   --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/merge_prod.py      --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25 --print-only
$W scripts/warehouse/check_staging.py   --project vigil-md-1792696816 --source maude --ingest-date 2026-10-25 --print-only
```

- To load one day by hand, trigger `vigil_md_load` with `{"ingest_date": "YYYY-MM-DD"}`.
- To rebuild PROD for a source, trigger `vigil_md_full_load` with `{"sources": ["maude"], "rebuild_prod": true}` (`merge_prod.py --rebuild` refuses to run without `--yes-rebuild`).
- Full data pull to GCS without Airflow: `python scripts/acquisition/fetch_sources.py --bucket vigil-md-raw-nd --start-year 2016 --end-year 2026 --mode full` (`--dry-run` previews). The default `--mode incremental` raises until a full load has written the partition state.

---

## Known limitations and issues

- **MAUDE scope is 2016–2026 by design** (`START_YEAR` / `END_YEAR` in the DAGs). To extend it, change `END_YEAR` in `vigil_md_acquisition.py` and `vigil_md_full_load.py`.
- **Matching runs on the samples only** (DVC pipeline). Exact linking on the PROD tables as a DAG task is the next step; blocking and ML matching on the full data come with the model pipeline.
- **The daily DAG runs only while the machine and Docker are running.** A missed run happens when Airflow next runs (`catchup=False` runs the most recent interval).
- Open issues are tracked in [`docs/issue.md`](docs/issue.md). Examples: the parallel fetch tasks can overwrite `manifest_state.json` keys (#5), task logs appear only when a task ends (#8), and the manufacturer suffix rule turns `"Tesco"` into `"tes"` (#13).

---

## Changes

> **2026-10-27 — action needed after pulling:** unzip now runs in a new pool, `vigil_upload`. Create it once on your machine, or every unzip task stays queued:
> `docker compose exec airflow-scheduler airflow pools set vigil_upload 1 "GCS uploads (unzip)"`.
> Check with `docker compose exec airflow-scheduler airflow pools list` (you need `vigil_upload` and `vigil_warehouse`, 1 slot each). Also update your `.env` from `.env.example` (Great Expectations in the containers) and run `pip install -r requirements.txt` (pinned versions, Fairlearn).

### 2026-10-27 — Pipeline checks, scheduling, CI

- **Checks in the DAGs:** `validate` (schema, Great Expectations, statistics) and `check` (anomaly checks) between staging and the merge, in both warehouse DAGs. Exit code 3 fails the task without a retry. Enforced since 2026-10-27 (`CHECKS_ENFORCE = True`).
- **Statistics for every load:** saved by the `validate` task; empty partitions and cost limits no longer fail it.
- **Scheduling:** new pool `vigil_upload` for unzip; `validate` and `check` in `default_pool`; per-source priorities with `weight_rule="upstream"`. Small sources are ready in minutes instead of at the end of a run.
- **Bias mitigation:** `bias_mitigation.py` (reweighting, held-out check, Fairlearn); `docs/BIAS_REPORT.md` section 6.
- **CI** on GitHub Actions; **all direct dependencies pinned** in `requirements.txt`.
- **Fixed:** the DAG tests could reset the real Airflow database; the full load and the acquisition `trigger_load` failed for runs without a logical date (CLI runs); the setup commands used one machine's container names (they now use the Compose service names).

### 2026-10-26 — Bias slice analysis of PROD

- **New `scripts/analysis/bias_slices.py`:** UDI availability, primary-DI match rate, end-to-end link coverage and field completeness by PROD-column slices; read-only SELECTs on `vigil_md_prod`, with a dry-run cost guard and `maximum_bytes_billed`.
- **Results:** CSVs and `summary.json` in `data/interim/bias/` (git-ignored); report in [`docs/BIAS_REPORT.md`](docs/BIAS_REPORT.md). 37 mocked tests.

### 2026-10-25 — GCS → BigQuery warehouse (RAW → STAGING → PROD)

- **New BigQuery layers:** `vigil_md_raw.*_raw`, `vigil_md_staging.*_stg`, `vigil_md_prod.*`, `vigil_md_ops.load_log` (`scripts/warehouse/`).
- **Incremental fetch:** `fetch_sources.py --mode incremental` (default) with per-partition fingerprint and SHA-256 checks, pending manifests, and a server-side copy for unchanged files.
- **GUDID:** the XML release is converted to NDJSON during the fetch, with part and record counts checked against the XML header.
- **Gzip NDJSON** in the unzip step: GUDID's conversion went from 4 h 53 min to 12 min 34 s.
- **DAGs:** acquisition fetches only changed sources and starts the new `vigil_md_load`; new manual `vigil_md_full_load`.
- **Tests:** 238 new mocked tests, shared in-memory GCS / HTTP fakes, and a Logstash guard.
- **Fixed:** a failed conversion could leave a partial NDJSON file in GCS (`docs/issue.md` #12); the GUDID MERGE bounds query used an untyped NULL.

---

## Roadmap

- [x] Sample data pull, preprocessing and normalization (all 4 sources)
- [x] Exact linking on the samples (108 MAUDE–GUDID links)
- [x] Full bulk download (2016–2026) and incremental RAW → STAGING → PROD warehouse
- [x] Airflow DAGs with Docker; ELK logging; DVC sample pipeline
- [x] Schema validation, statistics and anomaly checks in the pipeline, with alerts
- [x] Flow optimization (gzip NDJSON; pools and priorities from Gantt charts)
- [x] Bias slice analysis and mitigation (`docs/BIAS_REPORT.md`)
- [x] Tests in CI; pinned dependencies
- [ ] Exact linking on the PROD tables as a DAG task
- [ ] Candidate generation (blocking) and ML matching on the full data
- [ ] Unified BigQuery database with provenance
- [ ] Chat interface for recall history search

---

## Disclaimer

Vigil MD is a course project for research and educational purposes. It is not a medical tool. FDA data from openFDA is unvalidated, and adverse event reports alone do not establish that a device caused an outcome. Always consult a clinician and the official FDA source for decisions about medical devices.
