# Vigil-MD acquisition issues

Found in a review of `dags/vigil_md_acquisition.py` and `scripts/acquisition/fetch_sources.py` on 2026-10-25. Each issue has a status and an owner step in `docs/WAREHOUSE_PLAN.md` where one exists.

| # | Status | Area |
|---|---|---|
| 1 | Resolved 2026-10-25 | Worker credentials |
| 2 | Known limitation (by design) | MAUDE year range |
| 3 | Fixed (Step 7) | Fetch scope |
| 4 | Fixed (Step 7); rewritten files still reload | Full re-download |
| 5 | Open (partly mitigated) | State race |
| 6 | Fixed 2026-11-04 | GUDID date parsing |
| 7 | Fixed 2026-11-04 | GUDID RSS order |
| 8 | Fixed 2026-11-04 | Live task logs |
| 9 | Fixed 2026-11-04 | Summary counts |
| 10 | Fixed 2026-11-04 | Unused code |
| 11 | Fixed for the daily run (2 follow-ups open) | GUDID egress |
| 12 | Fixed, confirmed on real GCS | Partial NDJSON on error |
| 13 | Fixed 2026-11-03 (code and STAGING/PROD rows) | Manufacturer name normalization |
| 14 | Fixed 2026-11-04 | DAG helpers copied in 3 files |
| 15 | Fixed 2026-11-04 | Several pending exports of one partition |

---

## 1. Worker credentials file was missing — Resolved

- **Where:** `docker-compose.yaml:84`, `:94` (mounts `~/.config/gcloud` read-only into the worker).
- **Problem:** `/home/airflow/.config/gcloud/application_default_credentials.json` did not exist in the worker, so every GCS call in the DAG would fail.
- **Resolution:** the file exists now (checked 2026-10-25). The user ran `gcloud auth application-default login` in WSL.

## 2. `END_YEAR = 2026` is a fixed value — Known limitation (by design)

- **Where:** `dags/vigil_md_acquisition.py` (`START_YEAR = 2016`, `END_YEAR = 2026`), passed to `fetch_sources.py --start-year/--end-year`. Same values in `dags/vigil_md_full_load.py`.
- **Not an issue (user decision, 2026-10-25):** the project scope is MAUDE 2016–2026 (README, "Data Sources"). The fixed range is intended.
- **Effect to know:** MAUDE quarters from 2027 on are not fetched. Enforcement, recall and GUDID are not filtered by year.
- **To extend the scope later:** change `END_YEAR` in both DAG files, then run `vigil_md_full_load` (or let the next daily run fetch the new quarters).

## 3. Any change fetches all four sources — Fixed (Step 7)

- **Where:** `check_manifest` in `dags/vigil_md_acquisition.py`.
- **Problem:** one new recall export also started the MAUDE and GUDID fetches.
- **Fix:** Step 7 (`docs/WAREHOUSE_PLAN.md`): `check_manifest` returns only the fetch tasks of the sources that changed. Test: `test_branch_fetches_only_changed_sources`.

## 4. A new export date downloads everything again — Fixed (Step 7); rewritten files still reload

- **Where:** `fetch_sources.py` full mode (blob path contains `export_date`).
- **Problem:** MAUDE 2016–2026 downloaded again about once a week.
- **Fix:** Step 2 added `--mode incremental`; Step 7 makes the daily DAG use it (test: `test_fetch_one_source_uses_incremental_mode`). Per partition: same fingerprint (records, size) in the openFDA manifest → server-side copy, no download; downloaded with the same zip SHA-256 → not loaded; same content hash as the partition state → `skip_load` in the unzip step.
- **Still happens:** when openFDA rewrites the files (new fingerprint and new bytes), they are downloaded and loaded again even if no PROD row changes. 2026-11-02/03: two MAUDE re-exports, 497 files loaded (about 7 h), 0 PROD rows inserted or updated. The partition state is written only after a file's load commits; both exports were unzipped before either was loaded, so 32 files with unchanged content were not skipped. Since #15, only the newest export of each partition is loaded.

## 5. Parallel tasks can overwrite each other's state — Open (partly mitigated)

- **Where:** `fetch_sources.py:552` (re-read, change, write `manifests/manifest_state.json`).
- **Problem:** four fetch tasks run in parallel. Nothing locks the file. If two tasks write at the same moment, one export date can be lost. The next run then sees a false change and fetches again. Time is wasted. No data is corrupted.
- **Mitigation already done:** the new partition state uses one file per source (`manifests/partitions/{source}.json`).
- **Fix idea:** write with `if_generation_match` and retry on a precondition failure, or one state file per source.
- **Also:** the pull manifest name `manifests/pull_{run_time}.json` can collide if two tasks start in the same second.

## 6. GUDID date parsing is in two places and the copies differ — Fixed 2026-11-04

- **Where (before the fix):** `check_manifest` in `dags/vigil_md_acquisition.py` (fallback `""`), `latest_gudid_url` in `fetch_sources.py` (fallback today's date).
- **Problem:** if the URL ever has no 8-digit date, every run sees a new snapshot and downloads GUDID again. Today's URLs have the date, so this does not happen now.
- **Fix:** one shared function, `pick_latest_gudid` in `scripts/acquisition/gudid_release.py`, used by `check_manifest` and `latest_gudid_url`. It raises if no link has an 8-digit date, so the task fails instead of guessing a snapshot. Tests: `tests/test_gudid_release.py`, `test_branch_raises_without_a_dated_gudid_release`.

## 7. The newest GUDID release is assumed to be first in the RSS feed — Fixed 2026-11-04

- **Where (before the fix):** `latest_gudid_url` in `fetch_sources.py`, and the same loop in `check_manifest`.
- **Problem:** the code takes the first item that ends in `.zip`. If the feed lists the oldest first, it fetches an old release. The feed order is not verified.
- **Fix:** `pick_latest_gudid` collects all items and picks the largest 8-digit date in the file name. On 2026-11-04 the feed listed one release (`gudid_full_release_20261102.zip`), so the old code was not wrong yet; the fix guards against a change in the feed.

## 8. Script logs appear only at the end — Fixed 2026-11-04

- **Where:** `run_script` (now in `dags/vigil_helpers.py`; before: a copy in each DAG, `capture_output=True`).
- **Problem:** a fetch or conversion that runs an hour shows nothing in Airflow until it ends.
- **Fix:** `run_script` reads the script's output line by line while it runs and logs each line (stdout and stderr merged, so the order is real). `PYTHONUNBUFFERED=1` is set for the script; without it, `print()` output arrives only at the end (checked by experiment). Lines from our logger keep their level (WARNING/ERROR/CRITICAL); other lines are INFO, so normal stderr no longer shows as a warning. If the task is stopped, the script is killed, as `subprocess.run` did. Limit: a multi-line log message keeps its level on its first line only. Tests: `tests/dags/test_vigil_helpers.py`.

## 9. The summary counts every old version — Fixed 2026-11-04

- **Where:** `log_summary` in `dags/vigil_md_acquisition.py`.
- **Problem:** it counts every zip under `raw/{source}/` across all export dates. The total grows with each export and does not show the current data.
- **Fix:** `log_summary` counts only the newest export folder per source and logs its date (same rule as `scripts/warehouse/common.pick_latest`). Test: `test_log_summary_counts_only_the_newest_export`.

## 10. Unused code — Fixed 2026-11-04

- **Where:** `fetch_sources.py` imports (`from email import parser`), `build_failure_message` in `dags/vigil_md_acquisition.py` (never called).
- **Problem:** clutter. The alert email does not name the failed task.
- **Fixed 2026-11-04:** the unused import is deleted (and `xml.etree`, no longer needed in `fetch_sources.py`).
- **Fixed 2026-11-04 (alert):** `alert_on_failure` lists the tasks in state `failed` (Airflow 3 `ti.get_task_states`, through the supervisor) in the subject and body, via `build_failure_message`. The list is "so far": the alert starts as soon as one task fails. If the lookup fails, the e-mail still goes out with "unknown". The real lookup is first exercised at the next real failure.

## 11. GUDID is read back out of GCS for the XML → JSON conversion — Fixed for the daily run

- **Where:** `scripts/warehouse/unzip_to_ndjson.py` (Step 3).
- **Problem:** the worker runs outside Google Cloud. The unzip step reads the full GUDID release back from GCS (egress) after the fetch wrote it (ingress).
- **Fix (user decision 2026-10-25):** convert during the fetch. Download the zip once to a local temp file, upload the original zip to `raw/gudid/{snapshot}/`, convert the XML from the local file, and stream the NDJSON to GCS. No read-back.
- **Still open after the fix:**
  - `--scope all` (full load with `refetch=false`) still converts from GCS. A later fix can reuse the NDJSON written by the fetch (its hashes are stored in blob metadata).
  - openFDA files (MAUDE is the largest) have the same read-back. **User decision 2026-10-25: keep as is for now.** Fetch-time conversion is for GUDID only (`gudid_convert.py`).
- **Test on real GCS (2026-10-25):** a sample of 2 parts × 50 devices (`gs://vigil-md-raw-nd/samples/gudid/gudid_sample_20261001.zip`, 17,315 bytes, built from about 2.1 MB of range reads). T1 load, T2 unchanged_hash and T3 unchanged_content passed. T4 found issue 12. After the fix, a second run passed T1–T4.
- **Wired in:** `fetch_sources.fetch_gudid` (incremental mode) calls `gudid_convert.fetch_and_convert` for a new or changed snapshot. Its pending entry already has `content_sha256`, `row_count` and `ndjson_path`, so `unzip_to_ndjson.py` does not read the zip back. Full mode is unchanged.

## 12. A failed conversion left a complete-looking NDJSON object in GCS — Fixed (confirmed on real GCS)

- **Where:** `scripts/acquisition/gudid_convert.py` (`convert_local_zip`) and `scripts/warehouse/unzip_to_ndjson.py` (`process_entry`). Both wrote with `blob.open("wt")`.
- **Problem:** in text mode, google-cloud-storage 3.14 wraps the `BlobWriter` in an `io.TextIOWrapper`. Its `close()` finalizes the upload even while an exception is in flight. `BlobWriter.__exit__`, which calls `terminate()`, is never reached. In the real-GCS test T4, a count mismatch raised the right error but left a finalized 276,004-byte NDJSON object with no metadata.
- **Fix:** write with `blob.open("wb")` and the new `convert.Utf8Writer` adapter (UTF-8 encode, no `close()` of its own). An error now reaches `BlobWriter.__exit__` → `terminate()`.
- **Note:** binary writes (`stream_to_gcs` in `fetch_sources.py`) were never affected. The local fake bucket did not model this behaviour. It now does.

## 13. Manufacturer name normalization has two suffix bugs — Fixed (code 2026-11-02, rows corrected 2026-11-03)

- **Where:** `scripts/preprocessing/flatten_to_parquet.py` (`SUFFIXES`, `normalize_manufacturer`). The staging SQL `norm_mfr` in `scripts/warehouse/build_staging.py` copies it exactly (parity checked on BigQuery, 2026-10-25), so prod has the same behaviour.
- **Problem 1:** the pattern `,?\s*(…|co\.?|…)$` allows zero spaces before the suffix. A name that ends in the letters "co" loses them: `"Tesco"` → `"tes"`, `"Costco"` → `"cost"`.
- **Problem 2:** `llc` has no optional dot. `"Foo, LLC."` → `"foo, llc"` (not stripped). `inc.`, `corp.`, `ltd.`, `co.` have the dot.
- **Effect:** wrong `manufacturer_name_norm` values. This affects entity matching (`scripts/matching/`).
- **Fix (2026-11-02):** a legal suffix is stripped only when it does not follow a letter or digit (`(^|[^a-z0-9])(…)$`), so `"Tesco"` stays `"tesco"`. The character before the suffix is kept: `"Gentec(Shanghai)Corporation"` → `"gentec(shanghai)"`. `llc` takes an optional dot. Exception: `llc` is also stripped when glued on (`"AcumedLLC"` → `"acumed"`), because no word ends in "llc". Python (`SUFFIXES`) and SQL (`norm_mfr` in `build_staging.py`) changed together; tests in `tests/test_preprocessing.py`.
- **First attempt and regression:** the first fix (PR #17) required a space or comma before the suffix. A read-only check on STAGING/PROD showed it stopped stripping suffixes after other punctuation (`"J.Morita Mfg.Corp"`, `"Mediana.Co.Ltd"`). The current pattern replaces it.
- **Parity check (2026-11-02):** a one-off script compared BigQuery `norm_mfr` with `normalize_manufacturer` on 48 names (new cases, older suffix cases, punctuation, edge cases): 0 mismatches. The script is not in the repo.
- **Known limits (accepted):** a glued `co` is kept (`"ScripHessCo"` → `"scriphessco"`), since stripping it would break `"Tesco"`; `"Vygon U.S.A"` → `"vygon u"`; `"Acme-Inc"` → `"acme-"`; only the last suffix is stripped (`"Mediana.Co.Ltd"` → `"mediana.co"`); an accented letter before the suffix counts as a boundary.
- **Display:** `manufacturer_name_norm` is a matching key, not for display. The complete name stays in `manufacturer_name_raw` (and RAW `payload`). Search should match on `_norm` and show `_raw`.
- **Rows corrected (2026-11-03):** a one-off targeted UPDATE (not in the repo) set `manufacturer_name_norm = norm_mfr(manufacturer_name_raw)` and recomputed `record_hash` in all STAGING and PROD tables, STAGING first, with the DAGs stopped. Rows changed: STAGING 215,754 (enforcement 582, recall 804, MAUDE 185,037, GUDID 29,331), PROD 200,133 (194, 201, 185,037, 14,701). A read-only check afterwards found `to_change = 0` and `hash_mismatch = 0` in all 8 tables, so the daily MERGE leaves these rows unchanged. Cost about 39 GB scanned (UPDATE plus check). Other columns were not changed.
- **Related (not a bug):** for an impossible date such as `20241399`, Python `normalize_date` returns the string `"2024-13-99"`. SQL `norm_date` returns NULL. The SQL behaviour is intended.

## 14. DAG helpers are copied in 3 DAG files — Fixed 2026-11-04

- **Where:** `run_script` and `alert_on_failure` in `dags/vigil_md_acquisition.py`, `dags/vigil_md_load.py` and `dags/vigil_md_full_load.py` (marked `# TODO: move to shared module`).
- **Problem:** a fix in one copy (for example issue #8, live logs, or #10, the failed-task list in the alert) must be made three times.
- **Fix:** `dags/vigil_helpers.py` (listed in `dags/.airflowignore`) holds `run_script`, `alert_on_failure` and `build_failure_message`; the 3 DAGs import them. The acquisition DAG gets the exit-3 branch too (its scripts never exit 3). A test checks that each DAG uses the shared functions.

## 15. Several pending exports of the same partition were all loaded — Fixed 2026-11-04

- **Where:** `unzip_to_ndjson.py` (scope `changed`) and `load_raw_to_bq.py` read every pending manifest; `common.ndjson_path` has no export date.
- **Problem:** when a load is delayed and openFDA publishes a new export, two versions of a partition are pending in one run. Both are converted to the same NDJSON path (the newer overwrites the older), and both are loaded.
- **Effect (ingest date 2026-11-03, MAUDE exports 2026-11-02 and 2026-11-03):** 497 files loaded for 249 partitions (about 3.5 h extra). The 248 loads of 2026-11-02 entries read the 2026-11-03 NDJSON (249 NDJSON objects, all written before the load started), so their `_source_file` and `_content_sha256` in RAW and `load_log` name the 2026-11-02 zip, but the rows are 2026-11-03 data. `check_maude` failed `raw_to_staged_gap` (49.9%) because RAW held two copies, and the merge was skipped. Row counts matched in all 248 pairs, so the row-count check did not stop the load; with a different count it would have failed. STAGING and PROD were correct: the manual merge on 2026-11-04 inserted or updated 0 rows.
- **Fix:** `common.drop_superseded`: the unzip step keeps only the newest `export_date` per partition across the pending manifests, removes older entries from their manifest, and deletes a manifest left empty. `load_raw_to_bq.py` then sees one version per partition. Tests: `test_drop_superseded_*`, `test_main_converts_only_the_newest_export_and_rewrites_the_older_manifest`, `test_main_deletes_a_manifest_whose_entries_are_all_superseded`.
- **Left as is:** the RAW rows of those 248 loads (ingest date 2026-11-03) are already staged; deleting them is a BigQuery write and not needed. `ndjson_path` keeps no export date (one version per run now).
