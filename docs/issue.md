# Vigil-MD acquisition issues

Found in a review of `dags/vigil_md_acquisition.py` and `scripts/acquisition/fetch_sources.py` on 2026-10-25. Each issue has a status and an owner step in `docs/WAREHOUSE_PLAN.md` where one exists.

| # | Status | Area |
|---|---|---|
| 1 | Resolved 2026-10-25 | Worker credentials |
| 2 | Known limitation (by design) | MAUDE year range |
| 3 | Planned (Step 7) | Fetch scope |
| 4 | Planned (Step 2 done, Step 7 wires it) | Full re-download |
| 5 | Open (partly mitigated) | State race |
| 6 | Open | GUDID date parsing |
| 7 | Open | GUDID RSS order |
| 8 | Open | Live task logs |
| 9 | Open | Summary counts |
| 10 | Open | Unused code |
| 11 | Fixed for the daily run (2 follow-ups open) | GUDID egress |
| 12 | Fixed, confirmed on real GCS | Partial NDJSON on error |
| 13 | Open | Manufacturer name normalization |
| 14 | Open | DAG helpers copied in 3 files |

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

## 3. Any change fetches all four sources — Planned (Step 7)

- **Where:** `dags/vigil_md_acquisition.py:165`.
- **Problem:** one new recall export also starts the MAUDE and GUDID fetches.
- **Fix:** Step 7 returns only the fetch tasks of sources that changed.

## 4. A new export date downloads everything again — Planned

- **Where:** `fetch_sources.py` full mode (blob path contains `export_date`).
- **Problem:** MAUDE 2016–2026 downloads again about once a week.
- **Fix:** Step 2 added `--mode incremental` (fingerprint + SHA-256 per partition). Step 7 makes the DAG use it.

## 5. Parallel tasks can overwrite each other's state — Open (partly mitigated)

- **Where:** `fetch_sources.py:552` (re-read, change, write `manifests/manifest_state.json`).
- **Problem:** four fetch tasks run in parallel. Nothing locks the file. If two tasks write at the same moment, one export date can be lost. The next run then sees a false change and fetches again. Time is wasted. No data is corrupted.
- **Mitigation already done:** the new partition state uses one file per source (`manifests/partitions/{source}.json`).
- **Fix idea:** write with `if_generation_match` and retry on a precondition failure, or one state file per source.
- **Also:** the pull manifest name `manifests/pull_{run_time}.json` can collide if two tasks start in the same second.

## 6. GUDID date parsing is in two places and the copies differ — Open

- **Where:** `dags/vigil_md_acquisition.py:154` (fallback `""`), `fetch_sources.py:387` (fallback today's date).
- **Problem:** if the URL ever has no 8-digit date, every run sees a new snapshot and downloads GUDID again. Today's URLs have the date, so this does not happen now.
- **Fix idea:** one shared function. Raise if no date is found.

## 7. The newest GUDID release is assumed to be first in the RSS feed — Open

- **Where:** `fetch_sources.py:380-391` (`latest_gudid_url`), and the same loop in `check_manifest`.
- **Problem:** the code takes the first item that ends in `.zip`. If the feed lists the oldest first, it fetches an old release. The feed order is not verified.
- **Fix idea:** collect all items and pick the largest 8-digit date.

## 8. Script logs appear only at the end — Open

- **Where:** `dags/vigil_md_acquisition.py:52-68` (`run_script`, `capture_output=True`).
- **Problem:** a fetch or conversion that runs an hour shows nothing in Airflow until it ends.
- **Fix idea:** stream the subprocess output line by line to the task log.

## 9. The summary counts every old version — Open

- **Where:** `dags/vigil_md_acquisition.py:206` (`log_summary`).
- **Problem:** it counts every zip under `raw/{source}/` across all export dates. The total grows with each export and does not show the current data.
- **Fix idea:** count only the newest export folder per source.

## 10. Unused code — Open

- **Where:** `fetch_sources.py:41` (`from email import parser`), `dags/vigil_md_acquisition.py:248` (`build_failure_message`, never called).
- **Problem:** clutter. The alert email does not name the failed task.
- **Fix idea:** delete the import. Use `build_failure_message` in `alert_on_failure` with the failed task ids.

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

## 13. Manufacturer name normalization has two suffix bugs — Open

- **Where:** `scripts/preprocessing/flatten_to_parquet.py:32-46` (`SUFFIXES`, `normalize_manufacturer`). The staging SQL `norm_mfr` in `scripts/warehouse/build_staging.py` copies it exactly (parity checked on BigQuery, 2026-10-25), so prod has the same behaviour.
- **Problem 1:** the pattern `,?\s*(…|co\.?|…)$` allows zero spaces before the suffix. A name that ends in the letters "co" loses them: `"Tesco"` → `"tes"`, `"Costco"` → `"cost"`.
- **Problem 2:** `llc` has no optional dot. `"Foo, LLC."` → `"foo, llc"` (not stripped). `inc.`, `corp.`, `ltd.`, `co.` have the dot.
- **Effect:** wrong `manufacturer_name_norm` values. This affects entity matching (`scripts/matching/`).
- **Fix idea:** require a word boundary before the suffix (`(^|[\s,])(…)$`) and add `llc\.?`. Change Python and SQL together, then re-run the parity check.
- **Related (not a bug):** for an impossible date such as `20241399`, Python `normalize_date` returns the string `"2024-13-99"`. SQL `norm_date` returns NULL. The SQL behaviour is intended.

## 14. DAG helpers are copied in 3 DAG files — Open

- **Where:** `run_script` and `alert_on_failure` in `dags/vigil_md_acquisition.py`, `dags/vigil_md_load.py` and `dags/vigil_md_full_load.py` (marked `# TODO: move to shared module`).
- **Problem:** a fix in one copy (for example issue #8, live logs, or #10, the failed-task list in the alert) must be made three times.
- **Fix idea:** one helper module that all three DAGs import. Check that the DAG processor's import path can reach it, and that `tests/dags/` still parse every file.
