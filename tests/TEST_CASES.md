# Vigil-MD test cases

170 collected test cases across 8 files. All GCP calls are mocked (`fake_gcp` in `conftest.py`), except `test_raw_data_integration.py`, which talks to real GCS. That file is marked `integration` and deselected by default (see `pytest.ini`).

## How to run

| Scope | Command (from the repo root) |
|---|---|
| Everything, including DAG tests | `~/.venvs/vigil-airflow/bin/python -m pytest tests` |
| Without Airflow (DAG tests skip) | `~/.venvs/vigil-test/bin/python -m pytest tests` |
| Real-GCS integration checks only (needs credentials) | `~/.venvs/vigil-airflow/bin/python -m pytest tests -m integration` |

`test_data_quality.py` and `test_preprocessing.py` need pandas, which neither venv has yet. Until it's installed, add `--ignore=tests/test_data_quality.py --ignore=tests/test_preprocessing.py`.

## Summary

| File | Stage | Cases | Status |
|---|---|---|---|
| `test_storage_upload.py` | JSON → NDJSON → GCS | 17 | pass |
| `test_warehouse_load.py` | Parquet → GCS → BigQuery (input and CLI) | 8 | pass |
| `test_warehouse_load_mocked.py` | Parquet → GCS → BigQuery (mocked GCP calls) | 15 | pass |
| `test_data_contract.py` | BigQuery contract on `data/interim/*.parquet` | 50 | skipped unless `data/interim` is pulled (`dvc pull`) |
| `dags/test_acquisition_dag.py` | Airflow `pull_samples` DAG | 15 | pass |
| `test_data_quality.py` | Data quality on `data/interim/*.parquet` | 19 | not run (pandas not installed in test venvs) |
| `test_preprocessing.py` | Normalization helpers | 25 | not run (pandas not installed in test venvs) |
| `test_raw_data_integration.py` | Raw files in the `vigil-md-raw-nd` GCS bucket | 21 | deselected by default (integration); run by the `validate_gcs` task |

## 1. `test_storage_upload.py`: JSON → NDJSON

| Test | Cases | What it checks |
|---|---|---|
| `test_invalid_json_raises` | 2 | Empty file and malformed JSON raise `JSONDecodeError` |
| `test_missing_file_raises` | 1 | Missing source file raises `FileNotFoundError` |
| `test_invalid_wrapper_raises` | 4 | `{}`, wrong key, top-level list, and top-level scalar raise `ValueError` mentioning `records` |
| `test_invalid_records_raise` | 6 | `records` that is null, a dict, or a string ("must be a list"), empty ("empty"), or contains a non-object ("record N") |
| `test_conversion_preserves_records` | 1 | One valid JSON line per record; embedded newlines, unicode, nulls, and large ints survive; output path is correct; trailing newline |
| `test_failed_conversion_preserves_existing_output` | 1 | A failed conversion does not touch an existing output file |
| `test_rerun_overwrites_output` | 1 | A rerun replaces the previous output |
| `test_main_requires_bucket` | 1 | Missing `--bucket` exits with code 2 |

## 2. `test_warehouse_load.py`: input files and CLI

| Test | Cases | What it checks |
|---|---|---|
| `test_missing_required_args_exit` | 3 | No args, only `--project`, or only `--bucket` exits with code 2 |
| `test_missing_required_file_fails_before_upload` | 4 | Each of `maude_flat`, `links_maude_gudid`, `recall_flat`, `gudid_flat` missing raises `FileNotFoundError` naming the file, with no blob uploaded |
| `test_all_missing_files_are_reported_together` | 1 | Several missing files are listed in one error, with no upload |

## 3. `test_warehouse_load_mocked.py`: mocked GCS and BigQuery

| Test | What it checks |
|---|---|
| `test_all_input_files_present_pipeline_succeeds` | Happy path prints the success summary and all table names |
| `test_correct_gcs_bucket_selected` | The configured bucket is used |
| `test_correct_blob_names` | Blob names are `interim/<source>_flat.parquet`, the links file, and the report |
| `test_correct_local_paths_uploaded` | `upload_from_filename` gets the expected local paths |
| `test_linking_report_is_optional` | A missing `linking_report.json` is skipped, not an error |
| `test_gcs_upload_failure_propagates_and_skips_bigquery` | A GCS error propagates and BigQuery is never called |
| `test_expected_load_jobs_submitted` | One load job per table, each awaited with `.result()` |
| `test_correct_bigquery_project_and_destination_tables` | Client project, dataset (US, `exists_ok`), and URI → table mapping |
| `test_load_config_uses_parquet_and_truncate` | Parquet source format and `WRITE_TRUNCATE` on every job |
| `test_each_load_job_receives_its_config` | Each load call receives the job config |
| `test_bigquery_job_failure_propagates` | A failed load job raises |
| `test_zero_row_source_table_fails_loudly` | A source table that loads 0 rows raises `RuntimeError` |
| `test_zero_row_links_table_fails_loudly` | The links table loading 0 rows raises `RuntimeError` |
| `test_storage_client_init_failure_stops_pipeline` | A storage client error stops the run before BigQuery |
| `test_bigquery_client_init_failure_stops_before_loading` | A BigQuery client error means no load jobs run |

## 4. `test_data_contract.py`: BigQuery contract (real local files, no GCP)

Run once per file (`enforcement`, `recall`, `maude`, `gudid`, `links_maude_gudid`) unless noted. A file that hasn't been generated is skipped.

| Test | Cases | What it checks |
|---|---|---|
| `test_file_is_valid_parquet_with_rows` | 5 | Readable and not empty |
| `test_metadata_row_count_matches_rows_read` | 5 | Parquet metadata row count equals rows read |
| `test_column_names_are_legal_for_bigquery` | 5 | Names match `[A-Za-z_][A-Za-z0-9_]*`, at most 300 characters |
| `test_no_reserved_column_prefixes` | 5 | No `_TABLE_`, `_FILE_`, `_PARTITION`, and similar prefixes |
| `test_no_duplicate_column_names_case_insensitive` | 5 | No duplicates ignoring case |
| `test_column_types_are_supported_by_bigquery` | 5 | No null type, nested arrays, uint64, nanosecond timestamps, or maps. Previously failed for GUDID on test-cases data (`publish_date` typed `null`); not re-verified here (no local interim files) |
| `test_required_columns_present` | 5 | Required columns per table exist |
| `test_source_record_id_never_null_or_empty` | 5 | No null or blank IDs |
| `test_flat_source_column_matches_table` | 4 | `source` column equals the table name |
| `test_raw_json_is_valid_json` | 4 | `raw_json` parses as JSON in every row |
| `test_links_reference_existing_gudid_devices` | 1 | Every link `udi_di` exists in GUDID |
| `test_links_reference_existing_maude_records` | 1 | Every link `source_record_id` exists in MAUDE |

## 5. `dags/test_acquisition_dag.py`: Airflow DAG

Skipped when Airflow isn't installed. Level 3 tests run with `dag.test()`, with HTTP faked and output in a temp dir.

| Group | Test | What it checks | Status |
|---|---|---|---|
| A. Parsing | `test_dag_files_import_without_errors` | DagBag has no import errors | pass |
| A | `test_pull_samples_dag_is_found` | `pull_samples` is present | pass |
| A | `test_dag_ids_are_unique_across_dag_files` | No duplicate `dag_id` across files | pass |
| A | `test_parsing_has_no_side_effects` | Parsing creates no files or directories | pass |
| D. BUCKETS | `test_bucket_names_are_unique` | Unique bucket names | pass |
| D | `test_buckets_match_downstream_script_sources` | Names equal `SOURCES` in the upload and load scripts | pass |
| D | `test_bucket_kinds_are_valid` | Kinds are `openfda` or `gudid` | pass |
| D | `test_openfda_buckets_have_endpoint_and_gudid_does_not` | Endpoint presence matches the kind | pass |
| D | `test_only_maude_is_scoped_by_product_code` | Only `maude` is scoped | pass |
| L3. Execution | `test_full_run_succeeds_with_one_mapped_task_per_bucket` | Run succeeds with 4 mapped tasks plus summary | pass |
| L3 | `test_output_files_written_for_every_bucket` | A sample file per bucket and a correct `summary.json` | pass |
| L3 | `test_param_overrides_reach_tasks` | `limit` and `product_code` params reach requests and the summary | pass |
| L3 | `test_only_maude_requests_carry_product_code_search` | Only MAUDE requests have the product-code search | pass |
| L3 | `test_failure_in_one_bucket_fails_run_and_skips_summary` | A failed bucket fails the run and skips `write_summary` | pass |
| L3 | `test_zero_records_fails_loudly` | 0 records fails the run and writes no summary | pass |

## 6. `test_data_quality.py`: data quality (real local files, no GCP)

Parametrized tests run once per source (`enforcement`, `recall`, `maude`, `gudid`); a missing file is skipped. This overlaps with the contract file on purpose: it checks fields and duplicate IDs that the contract doesn't.

| Test | Cases | What it checks |
|---|---|---|
| `test_row_count_in_expected_range` | 4 | Row count within the expected range per source |
| `test_no_duplicate_source_record_ids` | 4 | `source_record_id` is unique |
| `test_required_fields_exist` | 4 | Source-specific required fields (e.g. `record_date`, `recall_class`) exist |
| `test_required_fields_not_all_null` | 4 | No required field is more than 90% null |
| `test_maude_udi_coverage` | 1 | At least 40% of MAUDE records have a `udi_di` |
| `test_manufacturer_norm_coverage` | 1 | At least 50% of records have `manufacturer_name_norm` |
| `test_record_date_coverage` | 1 | `record_date` coverage of at least 80% (75% for recall) |

## 7. `test_preprocessing.py`: normalization helpers

| Group | Cases | What it checks |
|---|---|---|
| `TestManufacturerNormalization` | 13 | Strips `Inc`, `Incorporated`, `Corp`, `Corporation`, `LLC`, `Ltd`; lowercases; collapses whitespace; variants of one company match; empty, `None`, and whitespace-only give `None`; subsidiary names are preserved |
| `TestModelKey` | 6 | Hyphens and spaces removed, case-insensitive, different models stay distinct, `None` and empty give `None` |
| `TestNormalizeDate` | 6 | `YYYYMMDD`, `MM/DD/YYYY`, and ISO inputs parse; `None`, empty, and invalid formats give `None` |

## 8. `test_raw_data_integration.py`: raw files in GCS (real GCP, integration)

Runs only with `-m integration`, and needs `google-cloud-storage` and credentials for `vigil-md-raw-nd`. The `validate_gcs` task in `dags/vigil_md_acquisition.py` runs this file after the fetch. It sets `VIGIL_START_YEAR` and `VIGIL_END_YEAR` (default 2016 and 2026) and fails if any test skips.

| Test | Cases | What it checks |
|---|---|---|
| `test_enforcement_folder_exists` | 1 | Enforcement folder has files |
| `test_enforcement_files_are_zips` | 1 | Enforcement files are zips |
| `test_enforcement_files_not_empty` | 1 | Enforcement files are non-empty |
| `test_recall_folder_exists` | 1 | Recall folder has files |
| `test_recall_files_not_empty` | 1 | Recall files are non-empty |
| `test_maude_year_exists` | 11 | MAUDE files exist for each year in the range. A missing final year only warns (partial year) |
| `test_maude_files_not_empty` | 1 | At least 30 MAUDE zips, none empty |
| `test_gudid_folder_exists` | 1 | GUDID folder has files |
| `test_gudid_file_is_large` | 1 | GUDID zips total over 100 MB |
| `test_manifest_state_exists` | 1 | `manifests/manifest_state.json` exists |
| `test_manifest_has_all_export_dates` | 1 | The manifest records every export date |

## Known open issues

1. **Null-typed Parquet column (unverified here):** on test-cases data, `gudid_flat.parquet` had `publish_date` typed `null`, which BigQuery probably can't load. If it recurs, the fix belongs in `flatten_to_parquet.py`: cast all-null columns to an explicit type.
2. **Relative path in data-quality tests:** `test_data_quality.py` reads `data/interim` relative to the working directory, so it must run from the repo root.

## Not covered

- Real BigQuery behavior, and real GCS outside the opt-in integration file (deliberately avoided; see the mocking note at the top).
- `flatten_to_parquet.py` is covered only through its helper functions in `test_preprocessing.py`.
- `pull_samples.py` and `exact_linking.py` have no unit tests yet.
