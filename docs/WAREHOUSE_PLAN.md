# Vigil-MD Warehouse Plan: RAW → STAGING → PROD (full and incremental)

## Context

The pipeline downloads FDA data to GCS every day (`dags/vigil_md_acquisition.py`, 06:00, `catchup=False`). Nothing loads that GCS data into BigQuery. The only BigQuery loaders (`load_samples_to_bq.py`, `load_interim_to_bq.py`) load small local sample files with `WRITE_TRUNCATE`. Nothing is incremental.

Also, when any export date changes, the DAG downloads all four sources again for 2016–2026.

Target:

1. **RAW**: load each JSON file into a date-partitioned BigQuery table. Do not transform it.
2. **STAGING**: read one day of raw data. Flatten nested arrays, cast types, and deduplicate.
3. **PROD**: MERGE (upsert) staging into the production tables.

Required properties:

- **Incremental ingestion**: download only new or changed partitions.
- **Content-hash checks**: skip a file whose content did not change, even if its export path is new.
- **Partition pruning**: staging and MERGE queries read only the relevant partitions.
- **Idempotent loading**: a second run of the same export does not add duplicate raw rows.

Two modes use the same scripts:

- **Incremental**: the daily DAG (`vigil_md_acquisition`, edited).
- **Full load**: a new manual DAG (`vigil_md_full_load`), for the first load, a backfill or a rebuild.

## Decisions (locked: do not change them without the user)

| # | Decision |
|---|---|
| D1 | One set of scripts with a `--scope changed\|all` flag. Do not write separate full and incremental scripts. |
| D2 | Unchanged partitions: **server-side copy** in GCS into the new `export_date` folder. No download. No reload into BigQuery. The folder layout stays complete, so `validate_gcs` still passes. |
| D3 | A raw table keeps each record in one `payload` JSON column, with no transformation. |
| D4 | Datasets: `vigil_md_raw` (exists, add new tables), `vigil_md_staging`, `vigil_md_prod`, `vigil_md_ops` (all new). Location `US`. |
| D5 | **(Revised in Step 3.)** The GUDID bulk zip contains 210 XML parts (`FULLDownload_Part{n}_Of_{N}_{date}.xml`, about 100 MB each), not NDJSON. The unzip step converts each `<device>` element to one JSON record, with every field and value kept. openFDA files contain one `{"meta","results":[…]}` object. |
| D6 | Tests are out of scope for this plan. Do not edit any file in `tests/`. |
| D7 | Repo: `\\wsl.localhost\Ubuntu\home\hitika\Vigik-MD-airflow` only. Do not touch the `Vigil-MD` copy. |
| D8 | This plan is copied to `Vigik-MD-airflow/docs/WAREHOUSE_PLAN.md` in Step 0. The checklist is ticked in that file. |

---

## GUARDRAILS FOR THE IMPLEMENTING AGENT (read before every step)

### Stop rules
- G1. Do **one step at a time**. When a step is complete, write the Step Report (template at the end). Tick its boxes in `docs/WAREHOUSE_PLAN.md`. Then **STOP**.
- G2. Do not start step N+1 until the user writes **"approved step N"**. "Looks good" or silence is not approval. Ask.
- G3. If a step needs a file that is not in its "Allowed files" list, STOP and ask.
- G4. If a fact is unknown, STOP and ask. Examples: a JSON field path, the name of a file inside a zip, or a column type. Do not guess, and do not invent field names.
- G5. If the user rejects a step, fix only what they named. Then STOP again.

### No real cloud or network
- G6. Do not run any command that calls real GCS, BigQuery, openFDA or AccessGUDID. This includes:
  - `fetch_sources.py`, even with `--dry-run`, because it reads GCS state and calls the FDA
  - any new script without `--print-only`
  - `dag.test()` on the real DAGs
- G7. Every new script that calls GCP must have `--print-only`. That flag prints the DDL, SQL or file plan and exits **before** it creates a client. Verify scripts with that flag only.
- G8. Put the logic in **pure functions**: SQL rendering, zip conversion, hash compare, change detection. Call GCP only in thin wrappers and `main()`. This follows `build_failure_message` in `vigil_md_acquisition.py:248`.
- G9. Import `google.cloud` **inside functions**, never at module top. This follows `load_samples_to_bq.py:48`. DAG files must parse with no side effects (`tests/dags/test_acquisition_dag.py:42`).

### Do not change these
- G10. Do not edit or delete these files:
  - `load_samples_to_bq.py`, `load_interim_to_bq.py`, `upload_samples_to_gcs.py`, `pull_samples.py`
  - `flatten_to_parquet.py`, `schema_map.py`
  - `scripts/matching/*`, `dags/pull_samples_dag.py`, `tests/*`
  - `docker-compose*.yaml`, `requirements.txt`
- G11. In `vigil_md_acquisition.py`, do not change:
  - `schedule="0 6 * * *"` (line 317), `catchup`, `BUCKET`, `START_YEAR`, `END_YEAR` or `dag_id`
  - `alert_on_failure`, `validate_gcs` or `log_summary` bodies (you may set a trigger rule)
- G12. Never drop, truncate, `CREATE OR REPLACE` or overwrite these existing tables:
  - `vigil_md_raw.*_sample`
  - `vigil_md_processed.*`

  Create the new datasets and tables with `exists_ok=True` / `CREATE TABLE IF NOT EXISTS`.
- G13. `fetch_sources.py` must keep today's keys in `manifests/manifest_state.json`. `check_manifest` and `tests/test_raw_data_integration.py` read them:
  - `openfda_{enforcement,recall,event}_export_date`
  - `gudid_snapshot`, `last_pull`, `start_year`, `end_year`

### Write modes
- G14. A load into a **new** table may use `WRITE_TRUNCATE` only with a partition decorator (`table$YYYYMMDD`) or on a temporary table. Do not use plain `WRITE_APPEND` into a raw table. Raw writes go only through the delete-and-insert transaction (Step 4).
- G15. Prod writes only by `MERGE`. The one exception is `--rebuild` in Step 6.

### Code style
- G16. Match the repo style:
  - `argparse` CLI
  - `from scripts.logger import get_logger` with the `sys.path.insert(0, parents[2])` pattern (`fetch_sources.py:43-46`)
  - `# ── Section ──` comment rulers
  - type hints like those in `fetch_sources.py`
- G17. Fail loudly. These are errors that raise:
  - a pending file with zero records
  - a zip with an unexpected member count
  - a MERGE that leaves duplicate keys

  **Nothing pending** is not an error. Log it and exit 0.
- G18. Do not add dependencies. `ijson` is already in `requirements.txt`. Use `ijson.items(f, "results.item", use_float=True)`. Without `use_float`, ijson gives `Decimal` values, and `json.dumps` fails on them.
- G19. Do not run git commit or git push unless the user asks. Do not reformat code you do not change.

---

## Architecture

### GCS layout

| Path | Writer | Notes |
|---|---|---|
| `raw/{endpoint}/{export_date}/{quarter}/{file}.zip`, `raw/gudid/{snapshot}/{file}.zip` | fetch | Existing layout. It stays complete through download, rename or server-side copy (D2). |
| `_incoming/{endpoint}/{run_ts}/{quarter}/{file}` | fetch | Temporary. Deleted or moved after the hash compare. |
| `manifests/partitions/{source}.json` | fetch, load_raw | Partition state for one source. Each source has its own file, so parallel tasks cannot race. |
| `manifests/pending/{source}/{run_ts}.json` | fetch | Files that must load. load_raw deletes each one after its commit. |
| `ndjson/{source}/{ingest_date}/{partition_key}.ndjson.gz` | unzip | One line per record: `{"payload": <record>}`. A second run overwrites the same path, so it is idempotent. |

Partition state (`manifests/partitions/{source}.json`):

```json
{"2024q1/device-event-0001-of-0004.json.zip": {
   "gcs_path": "raw/event/2026-10-20/2024q1/device-event-0001-of-0004.json.zip",
   "records": 123456, "size_mb": 45.2,
   "zip_sha256": "…", "content_sha256": "…", "loaded_at": "…"}}
```

`records` and `size_mb` can be `null`, for example after a full load from existing GCS files. Null means "unknown". The fetch then downloads the file and compares hashes.

Pending entry:

```json
{"source": "maude", "partition_key": "…", "gcs_path": "…", "export_date": "…",
 "records": 123456, "size_mb": 45.2, "zip_sha256": "…"}
```

### BigQuery tables

| Table | Partition | Cluster | Columns |
|---|---|---|---|
| `vigil_md_raw.{source}_raw` | `_ingest_date` DATE (day) | `_source_file` | `_ingest_date`, `_source_file` STRING, `_source_partition` STRING, `_zip_sha256` STRING, `_content_sha256` STRING, `_loaded_at` TIMESTAMP, `payload` JSON |
| `vigil_md_staging.{source}_stg` | `_ingest_date` | merge key | flat columns (below), `record_hash` STRING, `_ingest_date`, `_loaded_at` |
| `vigil_md_prod.{enforcement,recall,maude}` | `_part_date` DATE = `COALESCE(record_date, DATE '1900-01-01')` | merge key | flat columns, `record_hash`, `first_loaded_at`, `last_updated_at` |
| `vigil_md_prod.gudid` | none | `udi_di` | same as above |
| `vigil_md_ops.load_log` | none | `source` | `source`, `source_file`, `zip_sha256`, `content_sha256`, `ingest_date`, `row_count` INT64, `mode` STRING, `loaded_at` TIMESTAMP |

`{source}` is one of `enforcement`, `recall`, `maude`, `gudid`.

GUDID has no date that stays fixed (`publish_date` changes), so its prod table has no date partition.

Flat columns (port from `flatten_to_parquet.py`, and keep the same column names):

| Source | Lines | Merge key |
|---|---|---|
| enforcement | 76-93 | `source_record_id` |
| recall | 102-118 | `source_record_id` |
| maude | 133-151 | `source_record_id`, `device_seq` |
| gudid | 173-187 | `udi_di` |

Type changes from the parquet version:
- `record_date` and `publish_date` become DATE.
- `raw_json` is dropped, because raw already has `payload`.
- MAUDE gets the new column `device_seq` INT64, from `UNNEST … WITH OFFSET`.

### Data flow

```
fetch (incremental)       → pending manifests + partition state (only when content is unchanged)
unzip_to_ndjson           → ndjson/… + content_sha256 (skip if content is unchanged)
load_raw_to_bq            → temp table → BEGIN TRANSACTION; DELETE file rows for the date; INSERT; COMMIT
                            → load_log row → partition state → delete the pending manifest
build_staging             → read raw WHERE _ingest_date=@d → flatten/cast/dedup → stg$YYYYMMDD (WRITE_TRUNCATE)
merge_prod                → MERGE with key + _part_date range → duplicate-key assert
```

---

## STEP CHECKLIST

Tick each box in `docs/WAREHOUSE_PLAN.md`. Stop after each step (G1, G2).

### ☑ Step 0: Put the plan in the repo
- **Allowed files:** create `docs/WAREHOUSE_PLAN.md` only.
- [x] Copy this plan to `Vigik-MD-airflow/docs/WAREHOUSE_PLAN.md`, with no changes.
- [x] Report, then STOP.

### ☑ Step 1: Datasets and tables
- **Allowed files:** create `scripts/warehouse/__init__.py`, `scripts/warehouse/create_tables.py`, `scripts/warehouse/schemas.py`.
- [x] Write `schemas.py`. It holds the schemas, partition settings and clustering for every table in "BigQuery tables", as plain Python data. Other steps import it.
- [x] Write `create_tables.py`:
  - flags `--project` (required), `--print-only`
  - creates 3 datasets and 13 tables (4 raw, 4 staging, 4 prod, 1 load_log) with `exists_ok=True`
  - prints the plan with `--print-only`
- [x] Do not touch the existing tables (G12).
- [x] **Verify:** `python -m py_compile` both files. Run `create_tables.py --project vigil-md-1792696816 --print-only` and check that 13 tables print with the correct partitions.
- [ ] **User may run without `--print-only`:** the user, not the agent.
- [x] Report, then STOP.

### ☑ Step 2: Incremental fetch with content hash (`fetch_sources.py`)
- **Allowed files:** `scripts/acquisition/fetch_sources.py`.
- [x] Add `--mode incremental|full` near lines 239-251. Default `incremental`. `full` keeps today's behaviour exactly.
- [x] Add pure function `classify_partition(part, state_entry) -> "new"|"changed"|"same_fingerprint"`. It compares `records` and `size_mb`. A null value gives `"changed"`.
- [x] In the loop at lines 172-190 (incremental mode):
  - `same_fingerprint`: server-side copy `state_entry["gcs_path"]` to the new `blob_name` (`bucket.copy_blob`). Skip if the target exists. Do not add a pending entry.
  - `new` or `changed`: stream to `_incoming/…` and compute SHA-256. Reuse the streaming code from `stream_to_gcs` (lines 75-107). Add an optional target path, and do not duplicate the loop.
  - If the hash equals `state_entry["zip_sha256"]`: rename `_incoming` to `blob_name`. Update only `records` and `size_mb` in the partition state. Do not add a pending entry.
  - If the hash differs, or the partition is new: rename to `blob_name`. Add a pending entry.
- [x] Apply the same rules to GUDID (`fetch_gudid`, lines 213-234). The partition key is the fixed string `full_release` (not the zip file name, because the name carries the snapshot date). The fingerprint is `export_date` = snapshot. *(Approved by the user after Step 2.)*
- [x] In incremental mode, if `manifests/partitions/{source}.json` is missing: raise `RuntimeError("No partition state for {source}. Run vigil_md_full_load first.")`.
- [x] Write the pending manifest `manifests/pending/{source}/{run_ts}.json` only if it has entries.
- [x] Keep `manifests/manifest_state.json` and its keys exactly as today (G13). Keep `manifests/pull_*.json` (lines 310-318).
- [x] Do not write `zip_sha256` or `content_sha256` to the partition state when a file needs a load. Only load_raw writes them, after the commit.
- [x] **Verify:** `py_compile`. Call `classify_partition` with a small Python snippet: new, changed, same, and null fingerprint. Do not run `main()` (G6).
- [x] Report, then STOP.

> **Step 4 note (from Step 2):** load_raw must also copy `export_date` from the pending entry into the partition state. GUDID uses it as its fingerprint.

### ☑ Step 3: Unzip to NDJSON
- **Allowed files:** create `scripts/warehouse/unzip_to_ndjson.py`, `scripts/warehouse/common.py`.
- [x] `common.py`:
  - `SOURCES`, the endpoint map (`maude` ↔ `event`) and the dataset names
  - `read_pending(bucket, source)` and `list_all_latest(bucket, source)`
  - `--scope all`: for each partition, pick the file in the newest `export_date` / `snapshot` folder only. Never pick older folders.
- [x] Pure function `convert_zip(zip_fileobj, out_fileobj, source) -> (row_count, content_sha256)`:
  - openFDA: `ijson.items(member, "results.item", use_float=True)`.
  - GUDID (revised, see D5): read all `FULLDownload_Part{n}_Of_{N}_*.xml` members in part order with streaming `iterparse`. Convert each `<device>` to a dict:
    - Strip the namespace from tags. `xsi:nil="true"` becomes null. Leaf text is kept exactly as it is (no trim).
    - A plural wrapper whose children all share one tag (`identifiers`, `productCodes`, `gmdnTerms`, …) becomes `{child: [ … ]}`, always a list.
    - Any other repeated child tag becomes a list, so no data is lost.
  - GUDID checks: all parts 1..N present; device count per part equals the header `numberRecordXML`; the total equals `numberRecordsDatabase`. Raise on any mismatch.
  - Write `{"payload": record}` for each line.
  - Hash the canonical output bytes (`json.dumps(record, sort_keys=True, separators=(",", ":"))`).
  - openFDA: raise if the zip does not have exactly 1 `.json` member.
  - Raise if `row_count == 0`.
- [x] `main()`:
  - flags `--bucket`, `--source`, `--scope changed|all`, `--ingest-date`, `--print-only`
  - streams each GCS zip to `ndjson/{source}/{ingest_date}/{partition_key}.ndjson.gz`
  - if `content_sha256` equals the partition state value: mark the entry `skip_load` in the pending manifest, and update `zip_sha256` in the state
  - otherwise: write `content_sha256` and `row_count` to the pending entry
  - *(Added: `--scope all` writes its own `pending/{source}/full_<run_ts>.json`. Each entry also gets `ndjson_path`. The manifest is saved after each file, so a failed run resumes.)*
- [x] **Verify:** `py_compile`. Build a tiny zip in memory (2 openFDA records, then GUDID XML parts). Call `convert_zip` and check the count, the output lines and a stable hash. Run `--print-only` with no GCS call. Use the scratchpad for temporary files.
- [x] Report, then STOP.

> **Step 3 change (user decision, docs/issue.md #11):** in the daily (incremental) run, GUDID converts during the fetch, not in this step. `fetch_sources.fetch_gudid` → `scripts/acquisition/gudid_convert.py` downloads the zip once to a local temp file, uploads the original zip, and streams the NDJSON to `ndjson/gudid/{snapshot}/full_release.ndjson.gz`. Its pending entry arrives with `content_sha256`, so this step skips it. `--scope all` still converts GUDID from GCS. The shared pure conversion code moved to `scripts/warehouse/convert.py`. All NDJSON writes use `blob.open("wb")` + `Utf8Writer` (docs/issue.md #12). Tested on a real GCS sample (`samples/gudid/`).

> **Change (Oct 26, 2026, branch `airflow-optimize`):** NDJSON is now written gzip-compressed as `.ndjson.gz`, through `convert_zip_gz()` in `scripts/warehouse/convert.py`, by both the full-load unzip step and the daily GUDID conversion. BigQuery load jobs read gzip directly, so `load_raw_to_bq.py` and the load job are unchanged, and `content_sha256` is still computed over the uncompressed records. Measured: GUDID's file went from 12.94 GB to 463 MB, and `unzip_gudid` from 4 h 53 min to 12 min 34 s.

> **Step 5 note (from Step 3):** GUDID payload is the `<device>` dict at the top level (`$.brandName`, `$.identifiers.identifier[…]`), not `gudid.device` as in `flatten_gudid`. `_source_udi_di` does not exist in bulk data. The primary DI is `identifiers.identifier[deviceIdType="Primary"].deviceId`. Each record has a stable `publicDeviceRecordKey`. Decide the GUDID merge key with the user in Step 5.

### ☑ Step 4: Idempotent raw load
- **Allowed files:** create `scripts/warehouse/load_raw_to_bq.py`.
- [x] Flags: `--project`, `--bucket`, `--source`, `--scope changed|all`, `--ingest-date`, `--mode incremental|full`, `--print-only`.
- [x] For each pending entry that is not `skip_load`:
  1. If `load_log` has the same `source_file` and `content_sha256`: skip it, and log `already loaded`.
  2. Load the NDJSON to a temporary table `vigil_md_ops._tmp_{source}_{run_ts}` with `WRITE_TRUNCATE` and an explicit schema (`payload JSON`). Do not use autodetect.
  3. Run one multi-statement transaction:
     `BEGIN TRANSACTION; DELETE FROM raw WHERE _ingest_date=@d AND _source_file=@f; INSERT … SELECT @d, @f, @part, @zip, @content, CURRENT_TIMESTAMP(), payload FROM tmp; INSERT INTO load_log …; COMMIT TRANSACTION;`
  4. Drop the temporary table, and do it in `finally`.
  5. After the commit: write `zip_sha256`, `content_sha256`, `gcs_path`, `records`, `size_mb` and `loaded_at` to the partition state.
- [x] When all entries of a pending manifest are processed, delete that manifest.
- [x] Use query parameters for every value (`ScalarQueryParameter`). Do not use string formatting for values. Table names come only from `schemas.py`.
- [x] Raise if the inserted row count differs from `row_count` in the pending entry. *(Done inside the script, before `BEGIN TRANSACTION`, so a mismatch writes nothing.)*
- [x] **Verify:** `py_compile`. Run `--print-only` and check the rendered transaction SQL for one example file. Check that it has the `_ingest_date` filter in the DELETE.
- [x] Report, then STOP.

> **Step 4 additions:** `--scope changed` reads only daily manifests and `--scope all` reads only `full_*` manifests. An "already loaded" entry still refreshes the partition state, which repairs the state after a crash between commit and state save. Temp tables also get a 1-day expiry in case the `finally` drop fails. **BigQuery dry run (user-approved, 2026-10-25):** the full script and each statement pass. A DELETE without the `_ingest_date` filter is rejected by `require_partition_filter`, as intended.

### ☑ Step 5: Staging SQL
- **Allowed files:** create `scripts/warehouse/build_staging.py` and `scripts/warehouse/sql/staging_{enforcement,recall,maude,gudid}.sql`.
- [x] **Before you write GUDID SQL:** ask the user for 1 sample line of the bulk GUDID NDJSON (G4). The field paths in `flatten_gudid` (lines 158-191) come from the API sample pull (`_source_udi_di`, `gudid.device`). They can be wrong for the bulk files. openFDA bulk records have the same format as API records, so the enforcement, recall and MAUDE mappings stay valid.
- [x] Each SQL file:
  - `FROM raw WHERE _ingest_date = @ingest_date`. This is the only raw filter. It is required for pruning.
  - Extract with `JSON_VALUE(payload, '$.field')`.
  - MAUDE: `CROSS JOIN UNNEST(IFNULL(JSON_QUERY_ARRAY(payload,'$.device'), [JSON '{}'])) AS dev WITH OFFSET AS device_seq`. This keeps a report that has no devices, like `flatten_maude` line 131.
  - Dates: `COALESCE(SAFE.PARSE_DATE('%Y%m%d',x), SAFE.PARSE_DATE('%m/%d/%Y',x), SAFE.PARSE_DATE('%Y-%m-%d',x))`, the same as `normalize_date` (lines 49-61).
  - `manufacturer_name_norm`: the same rules as `normalize_manufacturer` (lines 39-46) and the `SUFFIXES` regex (lines 32-36): `LOWER`, `TRIM`, strip the suffix, trim `" ,."`, collapse whitespace, then `NULLIF(…,'')`.
  - Empty strings become NULL (`NULLIF`), like `clean()` (lines 64-68).
  - Dedup: `QUALIFY ROW_NUMBER() OVER (PARTITION BY <merge key> ORDER BY _loaded_at DESC) = 1`.
  - `record_hash = TO_HEX(SHA256(TO_JSON_STRING(STRUCT(<all business columns>))))`. Do not include the `_` columns.
- [x] `build_staging.py`:
  - flags `--project`, `--source`, `--ingest-date`, `--print-only`
  - writes to `{source}_stg${YYYYMMDD}` with `WRITE_TRUNCATE` *(changed: DELETE + INSERT for the date in one transaction, see the note below)*
  - exits 0 with a log line if the raw partition has no rows
- [x] **Verify:** `py_compile`. Run `--print-only` for each source and read the SQL. Confirm by eye that each column matches its `flatten_to_parquet.py` line.
- [x] Report, then STOP.

> **Step 5 decisions and changes (user-approved 2026-10-25):**
> - **GUDID merge key** is `publicDeviceRecordKey`, stored as `source_record_id` (`schemas.MERGE_KEYS`). The empty `gudid_stg` and prod `gudid` tables were re-clustered on `source_record_id`. `udi_di` is the Primary `deviceId`. GUDID text fields are trimmed, because raw keeps the XML text exactly.
> - **Null merge keys:** rows are dropped from staging with a warning and a count. Raw keeps them.
> - **Write mode:** a DELETE + INSERT transaction for `_ingest_date`, not `WRITE_TRUNCATE` to `stg$YYYYMMDD`. The staging tables have REQUIRED columns, and I expect a WRITE_TRUNCATE query load to fail on them, because query output columns are NULLABLE. The DML version is just as idempotent.
> - **MAUDE empty `device[]`:** `IFNULL` alone does not catch `[]`, so the SQL uses `IF(ARRAY_LENGTH(…) = 0, [JSON '{}'], …)`.
> - **Dedup tie-break:** `ORDER BY _loaded_at DESC, record_hash`, so a second run picks the same row.
> - **BigQuery checks:** all 4 staging scripts pass a dry run. The parity check on inline values (0 bytes read) matches `flatten_to_parquet.py` for every cell of enforcement, recall and MAUDE. `norm_date` and `norm_mfr` match Python on 24 test values. The one difference is intended: an impossible date gives NULL in SQL. Two old suffix bugs in Python are copied on purpose (docs/issue.md #13).

### ☑ Step 6: MERGE into prod
- **Allowed files:** create `scripts/warehouse/merge_prod.py` and `scripts/warehouse/sql/merge_{enforcement,recall,maude,gudid}.sql`.
- [x] `PRUNE_BY_DATE = {"enforcement": True, "recall": True, "maude": True, "gudid": False}`.
- [x] Compute `@min_d` and `@max_d` from the staging partition, as `_part_date` values.
- [x] MERGE pattern:
  ```sql
  MERGE prod T USING (SELECT … FROM stg WHERE _ingest_date=@ingest_date) S
  ON T.<key> = S.<key> [AND T._part_date BETWEEN @min_d AND @max_d]
  WHEN MATCHED AND T.record_hash != S.record_hash THEN UPDATE SET …, last_updated_at=CURRENT_TIMESTAMP()
  WHEN NOT MATCHED THEN INSERT (…, first_loaded_at, last_updated_at) VALUES (…, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
  ```
  Use the bracketed condition only if `PRUNE_BY_DATE` is true for the source.
- [x] Do not add `WHEN NOT MATCHED BY SOURCE … DELETE`. A partial day is not a full snapshot.
- [x] After the MERGE: run the duplicate check `SELECT COUNT(*) - COUNT(DISTINCT <key>)` on prod. Raise if it is greater than 0.
- [x] `--rebuild` (full-load DAG only): `CREATE OR REPLACE TABLE vigil_md_prod.{source}`. Use the partition and cluster settings from `schemas.py`. Select from all staging partitions, with dedup on the key. Before you run it, print the target and ask for `--yes-rebuild`.
- [x] Add a check query for date stability as text in `--print-only` output, for the user to run: keys with more than one `record_date` across raw history. If a source has such keys, set `PRUNE_BY_DATE` to False for it, after the user approves.
- [x] **Verify:** `py_compile`. Run `--print-only` for each source.
- [x] Report, then STOP.

> **Step 6 changes:**
> - **SQL generated from `schemas.py`** in `merge_prod.py`. The 4 `merge_*.sql` files are not created, because the MERGE differs only in columns and keys, and one generator keeps it in step with the tables.
> - **Duplicate check inside the transaction:** MERGE, check, and `ROLLBACK` + `RAISE` on duplicates. A failed run leaves prod unchanged.
> - **`--rebuild` keeps the table:** one transaction deletes every row and inserts the deduplicated rows of all staging partitions (`first_loaded_at` = the earliest staging `_loaded_at` per key). `CREATE OR REPLACE` would drop the REQUIRED modes and the table settings.
> - **Null-date rows:** the ON clause matches `_part_date BETWEEN @min_d AND @max_d OR _part_date = 1900-01-01`. The range excludes the sentinel, so one NULL date does not widen the range back to 1900.
> - **Date-stability check** reads staging history, not raw. Staging already has the typed `record_date` per key.
> - **BigQuery dry run (user-approved, after Step 9):** bounds, MERGE, rebuild and date-stability SQL for all 4 sources pass (15 dry runs). The first run found a bug: for GUDID (no date partition), the bounds query used an untyped `NULL`, which BigQuery types as INT64, so the comparison with a DATE failed. Fixed with `CAST(NULL AS DATE)` in `render_bounds_sql`.

### ☑ Step 7: Incremental DAG wiring (`vigil_md_acquisition.py`)
- **Allowed files:** `dags/vigil_md_acquisition.py`.
- [x] Docstring lines 1-15: describe the new tasks. Remove the "Teammate's DAG … BigQuery loading" line.
- [x] Config lines 36-41: add `PROJECT = "vigil-md-1792696816"` and `WAREHOUSE_POOL = "vigil_warehouse"`.
- [x] `check_manifest` lines 134-168:
  - Return only the `fetch.fetch_{source}` tasks of sources that changed (`event` → `maude`).
  - If nothing changed but any `manifests/pending/*` exists: return `"warehouse_start"`.
  - Otherwise return `"no_update"`.
- [x] `fetch_one_source` lines 176-185: pass `--mode incremental`.
- [x] `validate_gcs`: set `trigger_rule=TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS` on the operator (lines 361-364). When only some fetch tasks run, the others are skipped. Do not change the function body (G11).
- [x] Add `warehouse_start` (EmptyOperator, `NONE_FAILED_MIN_ONE_SUCCESS`).
- [x] Add TaskGroup `warehouse`. For each source, chain `unzip → load_raw → build_staging → merge_prod`:
  - Each task calls `run_script` (lines 52-68) with `--scope changed --ingest-date {{ ds }}`.
  - Each task uses `pool=WAREHOUSE_POOL`.
- [x] Dependencies (lines 380-382): `check >> [fetch_group, no_update, warehouse_start]`, then `fetch_group >> validate >> summary >> warehouse_start >> warehouse_group`. Add `warehouse_group` to the alert list.
- [x] Set `max_active_runs=1` on the DAG.
- [x] Do not change line 317 (G11).
- [x] **Verify:** parse the DAG with DagBag locally (no task run): `DagBag(dag_folder="dags").import_errors == {}`, and list the task ids. Confirm that parsing creates no files (the same rule as `test_parsing_has_no_side_effects`).
- [x] Report, then STOP.

> **Step 7 changes:**
> - **Branch return:** when sources changed, `check_manifest` returns the changed fetch tasks **and** `warehouse_start`. An Airflow branch skips every direct downstream task it does not return, and `warehouse_start` is one. If it were not returned, the warehouse would never run after a fetch. It still waits for `log_summary` (trigger rule `NONE_FAILED_MIN_ONE_SUCCESS`).
> - **ingest_date:** taken from `context["ds"]` in the callable, not a `{{ ds }}` template. The value is the same.
> - **Alert wiring:** `warehouse_group >> alert` links the last task of each chain (`merge_*`). An earlier failure makes `merge_*` `upstream_failed`, and `ONE_FAILED` fires on `failed` or `upstream_failed` (checked in the installed Airflow source).
> - **DAG description** updated (it said "into GCS" only). `dag_id`, schedule, `catchup`, `BUCKET` and the years are unchanged.

### ☑ Step 8: Full-load DAG
- **Allowed files:** create `dags/vigil_md_full_load.py`.
- [x] `dag_id="vigil_md_full_load"`, `schedule=None`, `catchup=False`, `max_active_runs=1`.
- [x] Params: `sources` (default all 4), `refetch` (bool, default False), `rebuild_prod` (bool, default False).
- [x] First task, `guard_no_acquisition_run`: fail if `vigil_md_acquisition` has a running DagRun.
- [x] If `refetch` is true: run `fetch_sources.py --mode full`.
- [x] Then, for each source:
  - `unzip --scope all`
  - `load_raw --scope all --mode full`
  - `build_staging`
  - `merge_prod`, with `--rebuild --yes-rebuild` only if `rebuild_prod` is true

  Every task uses `pool=WAREHOUSE_POOL`.
- [x] Copy `run_script` and the alert task from `vigil_md_acquisition.py`. Do not import from the other DAG file. Write `# TODO: move to shared module` above the copies.
- [x] **Verify:** DagBag parse with no import errors, and both dag_ids unique.
- [x] Report, then STOP.

> **Step 8 notes:**
> - **Guard** uses the Airflow 3 Task SDK `ti.get_dr_count(dag_id="vigil_md_acquisition", states=["running", "queued"])`, because tasks cannot query the metadata DB directly in Airflow 3. Retries are off for the guard.
> - **refetch=false** makes the `refetch` task a quick no-op (not a skip), so the load chains still run.
> - **Sources not in the `sources` param** raise `AirflowSkipException`, and the rest of that chain is skipped.
> - **Pool:** the guard is the only task outside `vigil_warehouse`.
> - **One-way guard:** the daily DAG does not check for a running full load. The 1-slot pool keeps BigQuery writers apart, but the daily fetch tasks are not in the pool. Do not unpause the daily DAG while a full load runs (see the Runbook).

### ☑ Step 9: Runbook (doc only)
- **Allowed files:** `docs/WAREHOUSE_PLAN.md` (append a "Runbook" section).
- [x] Write the go-live order for the user:
  1. `airflow pools set vigil_warehouse 1 "BigQuery writers"`
  2. `create_tables.py` (real run)
  3. Trigger `vigil_md_full_load`
  4. Check row counts
  5. Unpause `vigil_md_acquisition`
- [x] Explain why the order matters. Incremental fetch raises if there is no partition state. The full load writes that state.
- [x] Note: the first incremental run after a full load downloads each file once. The full load leaves `records` and `size_mb` as null, so the fetch must hash each file. Unchanged files are renamed in place and do not reload.
- [x] Report, then STOP.

### ☑ Step 10: Split the daily DAG into acquisition + loading (user request, after Step 9)
- **Decisions (user, 2026-10-25):**
  - Two daily DAGs: `vigil_md_acquisition` (FDA → GCS) and a new `vigil_md_load` (GCS → RAW → STAGING → PROD).
  - The loading DAG runs **only after the acquisition DAG is done**: the acquisition DAG's last task, `trigger_load`, is a `TriggerDagRunOperator`. It passes `conf={"ingest_date": "{{ ds }}"}`.
  - `trigger_load` runs when sources were fetched (after `log_summary`), or when pending manifests exist. On a quiet day (`no_update`), no loading run is made.
  - `vigil_md_full_load` stays as the separate manual DAG.
- **Allowed files:** `dags/vigil_md_acquisition.py`, `dags/vigil_md_load.py` (new), `dags/vigil_md_full_load.py` (guard only), `tests/dags/test_warehouse_dags.py`, `tests/TEST_CASES.md`, `docs/WAREHOUSE_PLAN.md`, `docs/issue.md`.
- [x] Acquisition DAG: replace `warehouse_start` + the `warehouse` group with `trigger_load`. Same branch rule. Keep G11 (schedule, `dag_id`, years, bucket).
- [x] `trigger_load`: `trigger_run_id="acq__{{ run_id }}"` + `skip_when_already_exists=True`, so a cleared or retried acquisition run does not start a second load.
- [x] Loading DAG: `schedule=None`, `catchup=False`, `max_active_runs=1`. One chain per source (unzip → load_raw → staging → merge) in pool `vigil_warehouse`. `ingest_date` from `dag_run.conf`, else the run's `ds`. Alert on failure.
- [x] Full-load guard: also fail if `vigil_md_load` has a run in progress.
- [x] Tests: update `tests/dags/test_warehouse_dags.py` for the new graph; add loading-DAG tests. Full suite green. *(47 DAG tests; full suite 293 passed, 50 skipped, 21 deselected.)*
- [x] Runbook: unpause `vigil_md_load` as well.
- [x] Report, then STOP.

> **Step 10 notes:**
> - The Step 7 wiring (`warehouse_start` and the `warehouse.*` group inside `vigil_md_acquisition`) is **replaced** by this step. The branch rule is the same; only the target changed (`trigger_load`).
> - `trigger_load` uses `NONE_FAILED_MIN_ONE_SUCCESS`: it runs after `log_summary`, or straight from the branch for pending files. A failed fetch, validation or summary means no loading run.
> - `resolve_ingest_date` reads `dag_run.conf` first, then the `ingest_date` param, then `ds`. It does not depend on the `dag_run_conf_overrides_params` setting.
> - If `vigil_md_load` is paused, `trigger_load` still creates its run. The run waits (queued) until the DAG is unpaused.
> - `run_script` and `alert_on_failure` are now copied in 3 DAG files (`docs/issue.md` #14).

### Out of scope (later, separate plan)
- ☑ Tests: **done** in `docs/TESTS_PLAN.md` (228 mocked tests, steps T1–T7; see `tests/TEST_CASES.md`).
  - mocked tests for the new scripts, with no real GCP
  - an update to `tests/test_warehouse_load_mocked.py` *(not needed: the old loader did not change)*
  - a DAG test for `vigil_md_full_load`
- `links_maude_gudid` in prod.
- Moving the DAG helpers to a shared module.

---

## Verification (whole plan)

The agent does these checks:
- `py_compile` on every changed or new file
- `--print-only` output for every script
- calls to the pure functions on in-memory data
- DagBag parse

The agent does not run real GCP checks (G6). The user runs them in the Step 9 runbook order. Then the user checks:

1. **Idempotent:** trigger the same `ingest_date` twice. The raw row count for that date is unchanged, and `load_log` has no new rows on the second run.
2. **Content hash:** run the incremental DAG twice with no FDA change. The second run takes `no_update`, or it creates no pending manifests.
3. **Pruning:** in the BigQuery job details for the staging and MERGE jobs, the bytes processed cover one raw partition and not the full table.
4. **Prod:** the duplicate-key check passes. The prod row counts are close to the `vigil_md_processed.*_flat` counts on the sample data.

## Step Report template (the agent fills it after each step)

```
Step N report
- Files changed: <path:lines>
- Files created: <path>
- What I verified: <commands and results>
- Guardrails I was close to: <none | Gx and why>
- Open questions for the user: <list or none>
- Waiting for: "approved step N"
```

---

## Runbook

Written 2026-10-25, after Steps 0–8. Run every command in a **WSL Ubuntu terminal**. Airflow runs in Docker; `vigik-md-airflow-airflow-scheduler-1` and `vigik-md-airflow-airflow-worker-1` are the container names.

### Before you start: state on 2026-10-25

| Item | State |
|---|---|
| `vigil_md_acquisition` | **Paused.** Keep it paused until step 5. Daily at 06:00 UTC: FDA → GCS, then starts `vigil_md_load` (Step 10). |
| `vigil_md_load` | New (Step 10). Not scheduled; started only by `vigil_md_acquisition.trigger_load`. Unpause it in step 5. |
| `vigil_md_full_load` | New, manual only (`schedule=None`). |
| BigQuery datasets and tables | **Created** (Step 1 real run). The `gudid_stg` and prod `gudid` tables are clustered on `source_record_id`. |
| Pool `vigil_warehouse` | **Not created yet.** |
| Partition state (`manifests/partitions/*.json`) | **Does not exist yet.** The full load writes it. |
| Worker credentials | Present (`docs/issue.md` #1). |

### Go-live order

**1. Create the pool (1 slot).**
```bash
docker exec vigik-md-airflow-airflow-scheduler-1 airflow pools set vigil_warehouse 1 "BigQuery writers"
docker exec vigik-md-airflow-airflow-scheduler-1 airflow pools get vigil_warehouse
```
Without this pool, the scheduler does not start any warehouse task.

**2. Create the tables.** This is already done. A second run is safe (`exists_ok=True`). It raises if a table has columns different from `schemas.py`.
```bash
docker exec -w /opt/airflow/Vigil-MD vigik-md-airflow-airflow-worker-1 \
  python scripts/warehouse/create_tables.py --project vigil-md-1792696816
```

**3. Run the full load.**
- In the Airflow UI, open `vigil_md_full_load` → **Trigger DAG w/ config**:
  ```json
  {"sources": ["enforcement", "recall", "maude", "gudid"], "refetch": false, "rebuild_prod": false}
  ```
- `refetch: false` uses the zips already in `gs://vigil-md-raw-nd/raw/`. Use `true` only if GCS is missing data.
- Tip: to test it first, trigger with `{"sources": ["recall"]}` (the smallest source), check step 4 for recall, then trigger again with the other sources.
- Keep `vigil_md_acquisition` and `vigil_md_load` **paused** while the full load runs. The acquisition fetch tasks are not in the pool. The full-load guard fails if either daily DAG has a run in progress.
- Run time is not measured yet. MAUDE (all quarters 2016–2026) and GUDID (about 5.2 million devices, 210 XML parts) are the slow parts. Logs appear only when each task ends (`docs/issue.md` #8). Tasks have no time limit.
- The GUDID full load converts the zip from GCS (one read of about 548 MB, `docs/issue.md` #11).

**4. Check the result.** In the BigQuery console (replace `D` with the run's date, for example `2026-10-26`):
```sql
-- raw, staging and prod counts per source (repeat for recall, maude, gudid)
SELECT 'raw' AS layer, COUNT(*) AS n FROM `vigil-md-1792696816.vigil_md_raw.enforcement_raw` WHERE _ingest_date = 'D'
UNION ALL SELECT 'staging', COUNT(*) FROM `vigil-md-1792696816.vigil_md_staging.enforcement_stg` WHERE _ingest_date = 'D'
UNION ALL SELECT 'prod', COUNT(*) FROM `vigil-md-1792696816.vigil_md_prod.enforcement`;

-- one load_log row per loaded file
SELECT source, COUNT(*) AS files, SUM(row_count) AS rows
FROM `vigil-md-1792696816.vigil_md_ops.load_log` GROUP BY source;

-- no leftover temp tables (they also expire after 1 day)
SELECT table_name FROM `vigil-md-1792696816.vigil_md_ops.INFORMATION_SCHEMA.TABLES`
WHERE table_name LIKE '\\_tmp\\_%';
```
And in GCS:
```bash
gcloud storage ls gs://vigil-md-raw-nd/manifests/partitions/   # 4 files: enforcement, recall, maude, gudid
gcloud storage ls -r gs://vigil-md-raw-nd/manifests/pending/   # empty (every manifest loaded)
```
Expected:
- raw = the sum of `row_count` in `load_log` for that source.
- staging ≤ raw for enforcement, recall and GUDID (duplicates and null keys removed). MAUDE staging can be larger than raw (one row per device).
- prod = staging on the first load.

**5. Unpause the two daily DAGs** (loading first, so the first trigger is not left queued).
```bash
docker exec vigik-md-airflow-airflow-scheduler-1 airflow dags unpause vigil_md_load
docker exec vigik-md-airflow-airflow-scheduler-1 airflow dags unpause vigil_md_acquisition
```
`vigil_md_acquisition` runs daily at 06:00 UTC. When it is done, its `trigger_load` task starts `vigil_md_load` with the same date. `vigil_md_load` has no schedule of its own.

### Why the order matters

- The incremental fetch (`fetch_sources.py --mode incremental`) **raises** if `manifests/partitions/{source}.json` does not exist. Only `load_raw_to_bq.py` writes that file, after its commit. So the full load must run before the daily DAG.
- The warehouse tasks need the pool (step 1). Without it, they stay unscheduled.

### What to expect from the first daily runs

- **First run after the full load:** the full load leaves `records` and `size_mb` empty (null) in the partition state. So the first incremental fetch of each changed source downloads each partition once, to compare hashes. Files with unchanged content are kept in place and **do not load again** (`unchanged_hash` / `skip_load`). Later runs skip the download when `records` and `size_mb` are unchanged (server-side copy instead).
- **No FDA change:** the acquisition run takes `no_update` and ends. No loading run is made.
- **A failed load:** the pending manifest stays in GCS. The next acquisition run goes to `trigger_load` even with no FDA change, and the new loading run retries it. A second load of the same file is skipped by `load_log`.
- **A failed acquisition** (fetch, validation or summary): no loading run is made for that day.

### Checks after a few days

- **Idempotency:** clear and re-run one day's `vigil_md_load` run (its `load.*` tasks) in the UI. Raw, staging and prod counts for that date do not change, and `load_log` gets no new rows.
- **Pruning:** in the BigQuery job details of a `staging_*` or `merge_*` job, the bytes processed cover one day of raw or the staging date range, not the whole table.
- **Date stability:** print the check with `python scripts/warehouse/merge_prod.py --project vigil-md-1792696816 --source maude --ingest-date D --print-only`, then run the last query in BigQuery. A result above 0 means `record_date` changes for some keys. Then set `PRUNE_BY_DATE[source] = False` in `merge_prod.py`, after a review.

### Recovery

| Problem | Action |
|---|---|
| A loading task failed | Fix the cause. Clear the failed task of that `vigil_md_load` run in the UI, or wait for the next acquisition run (it triggers a new load for pending files). Pending manifests and `load_log` make the retry safe. |
| Load one day by hand | Trigger `vigil_md_load` with `{"ingest_date": "YYYY-MM-DD"}`. |
| Prod looks wrong for a source | Trigger `vigil_md_full_load` with `{"sources": ["<source>"], "rebuild_prod": true}`. This replaces every prod row of that source from all staging history, in one transaction. |
| GCS is missing raw files | Trigger `vigil_md_full_load` with `"refetch": true` for that source. This downloads the full history again. |
| The MERGE raises "duplicate merge keys" | Nothing was written (rolled back). Usually `record_date` changed for a key; run the date-stability check and set `PRUNE_BY_DATE` to False for that source. |

### Open issues to keep in mind (`docs/issue.md`)

- **#2 (known limitation, by design):** the MAUDE scope is 2016–2026 (`END_YEAR = 2026`). Quarters from 2027 on are not fetched.
- **#5:** the four parallel fetch tasks can overwrite each other's `manifest_state.json` keys (wasted re-fetch, no data loss).
- **#8:** task logs appear only when a task ends.
- **#13:** manufacturer name normalization bugs (`"Tesco"` → `"tes"`).
- **Tests** for the new scripts and DAGs are not written yet (Out of scope).
