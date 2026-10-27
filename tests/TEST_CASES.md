# Vigil-MD test cases

408 collected test cases across 18 files. All GCP calls are mocked (`fake_gcp` and the in-memory `FakeBucket` in `conftest.py`), except `test_raw_data_integration.py`, which talks to real GCS. That file is marked `integration` and deselected by default (see `pytest.ini`). An autouse fixture (`no_logstash` in `conftest.py`) keeps test logs out of Logstash.

Files 9–18 cover the RAW → STAGING → PROD warehouse (`docs/WAREHOUSE_PLAN.md`), added by `docs/TESTS_PLAN.md`.

## How to run

| Scope | Command (from the repo root) |
|---|---|
| Everything, including DAG tests | `~/.venvs/vigil-airflow/bin/python -m pytest tests` |
| Without Airflow (DAG tests skip) | `~/.venvs/vigil-test/bin/python -m pytest tests` |
| Real-GCS integration checks only (needs credentials) | `~/.venvs/vigil-airflow/bin/python -m pytest tests -m integration` |

`test_data_quality.py` and `test_preprocessing.py` need pandas, which neither venv has yet. Until it's installed, add `--ignore=tests/test_data_quality.py --ignore=tests/test_preprocessing.py`.

The warehouse conversion tests need `ijson`. It is installed in `~/.venvs/vigil-airflow` (with `~/.local/bin/uv pip install --python ~/.venvs/vigil-airflow/bin/python ijson`; the venv has no pip).

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
| `test_fakes.py` | The shared test fakes and the Logstash guard | 13 | pass |
| `test_convert.py` | zip → `{"payload": record}` NDJSON (openFDA JSON, GUDID XML) | 17 | pass |
| `test_gudid_convert.py` | GUDID: download once, keep the zip, convert locally | 8 | pass |
| `test_fetch_incremental.py` | `fetch_sources.py` incremental mode | 29 | pass |
| `test_unzip_to_ndjson.py` | Unzip step and `--scope all` file list | 14 | pass |
| `test_load_raw_to_bq.py` | Idempotent RAW load into BigQuery (mocked) | 23 | pass |
| `test_schemas.py` | Warehouse table definitions | 24 | pass |
| `test_build_staging.py` | STAGING SQL (flatten, cast, dedup) | 30 | pass |
| `test_merge_prod.py` | MERGE into PROD and `--rebuild` | 33 | pass |
| `dags/test_warehouse_dags.py` | `vigil_md_acquisition` → `vigil_md_load` trigger, `vigil_md_load`, `vigil_md_full_load` | 47 | pass |
| `test_bias_slices.py` | Bias slice analysis of PROD (read-only SQL, statistics, outputs) | 37 | pass |

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

## 9. `test_fakes.py`: shared fakes (no GCP)

The fakes model google-cloud-storage 3.14. A fake that did not hid a real bug (`docs/issue.md` #12).

| Test | What it checks |
|---|---|
| `test_logstash_handler_never_connects` | With the autouse guard, logging never opens a Logstash connection |
| `test_wb_write_is_stored_on_success` / `test_wb_write_leaves_no_object_on_error` | `open("wb")`: stored on success, no object after an exception (BlobWriter terminate) |
| `test_wt_write_is_finalized_even_on_error` | `open("wt")`: finalized even after an exception (the real TextIOWrapper behaviour) |
| `test_rb_reads_and_missing_object_raises`, `test_upload_download_delete`, `test_copy_and_rename`, `test_metadata_patch_persists`, `test_list_blobs_filters_sorts_and_limits` | The rest of the FakeBucket / FakeBlob API |
| `test_session_routes_records_and_streams` | FakeSession routes, records calls, streams chunks, raises on 4xx/5xx |
| `test_openfda_zip_*`, `test_gudid_zip_*` | The zip builders produce the real file layouts and header counts |

## 10. `test_convert.py`: zip → NDJSON (no GCP)

| Test | What it checks |
|---|---|
| `test_openfda_payload_equals_source_records`, `test_openfda_floats_stay_floats` | Records come out unchanged; floats stay floats (`ijson use_float`) |
| `test_openfda_zero_records_raises`, `test_openfda_needs_exactly_one_json_member` (2) | 0 records, or 0 / 2 `.json` members, raise |
| `test_hash_is_stable_across_meta_key_order_and_member_name`, `test_hash_changes_when_one_record_changes` | The content hash ignores meta, key order and file name, and changes with any record |
| `test_gudid_device_fields`, `test_gudid_plural_wrappers_are_always_lists`, `test_xml_to_value_repeated_tags_and_attributes` | XML → dict: namespace stripped, `xsi:nil` → null, text exact, wrappers always lists, repeated tags → lists, attributes → `@name` |
| `test_gudid_parts_are_read_in_part_order`, `test_gudid_parts_rejects_missing_part`, `test_gudid_parts_rejects_mixed_totals` | Parts 1..N in order; a missing part or mixed totals raise |
| `test_gudid_part_count_must_match_header`, `test_gudid_total_must_match_header` | Device counts must match `numberRecordXML` and `numberRecordsDatabase` |
| `test_gudid_hash_is_stable_across_new_part_names` | A new release date in the part names does not change the hash |
| `test_utf8_writer_encodes_and_never_closes` | `Utf8Writer` writes UTF-8 and has no `close` |

## 11. `test_gudid_convert.py`: GUDID during the fetch (FakeBucket, FakeSession)

| Test | What it checks |
|---|---|
| `test_new_release_loads` | `load`: original zip kept, NDJSON written, hashes in blob metadata |
| `test_same_zip_bytes_copies_server_side_and_does_not_convert` | `unchanged_hash`: server-side copy, no NDJSON |
| `test_new_bytes_same_records_removes_ndjson` | `unchanged_content`: NDJSON removed, zip kept |
| `test_conversion_error_raises_and_leaves_no_ndjson` | Regression #12: an error leaves no NDJSON object |
| `test_ndjson_is_written_in_binary_mode` | Regression #12: the NDJSON blob is opened `"wb"`, never `"wt"` |
| `test_put_zip_skips_existing_object`, `test_fetch_and_convert_downloads_once_and_cleans_temp`, `test_ndjson_name_uses_snapshot_and_fixed_key` | No re-upload; one download; temp file removed; NDJSON path |

## 12. `test_fetch_incremental.py`: incremental fetch (FakeBucket, FakeSession, fake_gcp)

| Test | What it checks |
|---|---|
| `test_classify_partition` (6), `test_classify_partition_gudid_fingerprint`, `test_openfda_partition_key_is_quarter_and_file` | Change detection, including nulls and the GUDID release-date fingerprint |
| `test_same_fingerprint_copies_without_download`, `test_same_fingerprint_and_target_exists_is_skipped` | Unchanged partition: server-side copy or skip, 0 downloads |
| `test_changed_fingerprint_same_hash_is_not_pending`, `test_null_fingerprint_downloads_and_compares` | Same SHA-256 under a new path: nothing pending, `_incoming/` cleaned |
| `test_new_content_is_pending_and_state_untouched`, `test_new_partition_loads`, `test_dry_run_downloads_and_writes_nothing` | New content: pending entry; hashes not written before the raw load |
| `test_missing_partition_state_raises`, `test_empty_pending_writes_nothing`, `test_pending_and_partition_state_paths` | State helpers |
| `test_gudid_*` (6) | GUDID: converted pending entry, same snapshot not downloaded, `unchanged_content`, `unchanged_hash`, dry run, full mode does not convert |
| `test_main_incremental_checks_all_state_before_any_download`, `test_main_incremental_new_partition_writes_pending`, `test_main_full_mode_never_reads_partition_state`, `test_main_keeps_every_manifest_state_key` (2) | `main()` orchestration; `manifest_state.json` keeps every key |

## 13. `test_unzip_to_ndjson.py`: unzip step (FakeBucket, fake_gcp)

| Test | What it checks |
|---|---|
| `test_pick_latest_*` (4) | `--scope all`: newest export folder only, GUDID fixed key, 2 GUDID zips raise, empty |
| `test_apply_content_check` (3) | New / changed / unchanged → `skip_load` |
| `test_process_entry_*` (3) | NDJSON written and entry filled; zip hash mismatch raises; unchanged content refreshes pointers, never `content_sha256` |
| `test_main_changed_skips_entries_already_converted`, `test_main_all_writes_a_full_manifest`, `test_main_nothing_pending_writes_nothing` | `main()` scopes |
| `test_print_only_imports_no_gcp` | `--print-only` loads no `google.cloud` |

## 14. `test_load_raw_to_bq.py`: idempotent RAW load (fake_gcp, FakeBucket)

| Test | What it checks |
|---|---|
| `test_delete_is_limited_to_one_day_and_one_file`, `test_row_count_check_comes_before_the_transaction`, `test_statement_order`, `test_every_sql_parameter_is_bound`, `test_query_params_values` | SQL structure and parameters |
| `test_manifest_in_scope` (4), `test_check_entry_*` (3), `test_tmp_table_is_always_in_ops_dataset`, `test_state_after_load_has_the_hashes` | Helpers |
| `test_load_entry_loads_runs_script_and_drops_temp`, `test_load_entry_drops_temp_even_on_error`, `test_already_loaded_reads_the_count` (2) | BigQuery calls (mocked); temp table dropped in `finally` |
| `test_main_*` (5) | New file loads; already loaded → skipped, state healed; `skip_load` not loaded; each scope deletes only its own manifest; a load error keeps the manifest |

## 15. `test_schemas.py`: table definitions (no GCP)

| Test | What it checks |
|---|---|
| `test_thirteen_tables` | 4 raw, 4 staging, 4 prod, 1 load_log |
| `test_cluster_and_partition_fields_are_columns` (13), `test_merge_keys_are_staging_and_prod_columns` (4) | Every cluster, partition and merge key field exists; staging/prod cluster on the merge key |
| `test_raw_and_staging_require_a_partition_filter`, `test_prod_partitioning`, `test_gudid_merge_key_is_public_device_record_key` | Partitioning rules; GUDID key |
| `test_protected_tables_are_refused` (2) | `vigil_md_processed.*` and `*_sample` are refused |
| `test_create_tables_print_only_makes_no_gcp_call` | `create_tables.py --print-only` |

## 16. `test_build_staging.py`: STAGING SQL (fake_gcp)

| Test | What it checks |
|---|---|
| `test_source_sql_is_valid`, `test_insert_columns_match_schemas`, `test_reads_one_raw_partition_and_writes_one_staging_partition`, `test_dedup_and_null_key_filter`, `test_statement_order`, `test_record_hash_covers_business_columns_only` (4 each) | Per source: columns = `schemas.py`, one partition in and out, dedup on the merge key, null keys dropped, order, hash on business columns only |
| `test_maude_keeps_reports_without_devices` | Empty `device[]` keeps one row |
| `test_validator_rejects_bad_sql` (3) | Missing partition filter, missing column, missing raw-table token |
| `test_empty_raw_partition_runs_no_script`, `test_staging_script_runs_after_the_count` | `main()`: empty day → no script; counts in the summary |

## 17. `test_merge_prod.py`: MERGE and rebuild (fake_gcp)

| Test | What it checks |
|---|---|
| `test_prune_only_date_partitioned_sources` | `PRUNE_BY_DATE` only for date-partitioned prod tables |
| `test_on_clause`, `test_insert_columns_equal_prod_columns`, `test_update_changes_only_changed_rows_and_keeps_first_loaded_at`, `test_daily_merge_never_deletes`, `test_merge_and_duplicate_check_share_one_transaction`, `test_rebuild_keeps_the_table` (4 each) | Per source: ON clause, columns, UPDATE rules, no deletes, one transaction with the duplicate check, rebuild keeps the table |
| `test_bounds_for_gudid_uses_a_typed_null` | Regression: untyped NULL (INT64) vs DATE failed in BigQuery |
| `test_bounds_for_date_sources_skip_the_sentinel` | The NULL-date sentinel does not widen the range |
| `test_rebuild_without_confirmation_refuses`, `test_ingest_date_required_without_rebuild`, `test_empty_staging_partition_runs_no_merge` | CLI guards |
| `test_merge_gets_the_staging_date_range`, `test_all_null_dates_match_only_the_sentinel`, `test_gudid_merge_has_no_date_params` | Query parameters passed to the MERGE |

## 18. `dags/test_warehouse_dags.py`: warehouse DAGs

Skipped when Airflow isn't installed. No `dag.test()`: the tasks call real scripts.

| Test | What it checks |
|---|---|
| `test_all_warehouse_dags_parse` | The 3 DAGs load with no import errors |
| `test_acquisition_schedule_is_unchanged`, `test_acquisition_branch_targets`, `test_acquisition_trigger_rules`, `test_acquisition_has_no_warehouse_tasks`, `test_fetch_one_source_uses_incremental_mode` | Acquisition DAG: schedule, branch targets (`trigger_load`), trigger rules, no loading tasks left, incremental fetch |
| `test_trigger_load_starts_the_loading_dag_once` | `trigger_load` starts `vigil_md_load` with `conf={"ingest_date": "{{ ds }}"}`, a fixed run id and `skip_when_already_exists`, so a retried acquisition does not load twice |
| `test_branch_*` (4) | `check_manifest`: changed sources only, new GUDID snapshot, pending files only (→ `trigger_load`), no update |
| `test_load_dag_is_triggered_only`, `test_load_dag_chain` (4) | Loading DAG: no schedule, `max_active_runs=1`, 4 chains in pool `vigil_warehouse`, each chain → alert |
| `test_resolve_ingest_date` (3), `test_resolve_ingest_date_rejects_missing_or_bad` (2), `test_load_step_uses_the_triggered_ingest_date` | `ingest_date`: trigger conf, then param, then `ds`; missing or bad dates raise |
| `test_warehouse_args_parse` (4), `test_full_load_args_parse` (5), `test_full_load_args_modes` | Every argument list is accepted by its script (`--print-only`, subprocess) |
| `test_full_load_is_manual_with_params`, `test_full_load_chain` (4), `test_selected_sources_rejects_bad_params` (3) | Full-load DAG: manual, params, chains |
| `test_guard_passes_when_no_daily_run`, `test_guard_fails_when_a_daily_dag_runs` (2), `test_refetch_*` (2), `test_unselected_source_is_skipped`, `test_rebuild_flags_only_on_merge` | Guard (checks both daily DAGs), refetch, skip and rebuild logic |

## 19. `test_bias_slices.py`: bias slice analysis (fake_gcp, FakeClient)

The SQL is checked as text; `run()` uses a `FakeClient` that returns canned rows. Results: `docs/BIAS_REPORT.md`.

| Test | What it checks |
|---|---|
| `test_every_query_reads_only_prod`, `test_guard_rejects_non_prod_or_writes` (5), `test_guard_allows_columns_that_contain_keywords` | Every query is a SELECT on `vigil_md_prod` only; RAW, STAGING, MERGE, DELETE and CREATE are rejected |
| `test_maude_match_is_exact_against_distinct_gudid_primary_di`, `test_maude_length_rule_comes_from_expectations` | The matching rule is unchanged (exact `udi_di`, no TRIM/UPPER, DISTINCT GUDID DIs); length rule from `expectations.RULES` |
| `test_shape_query_only_classifies_unmatched_rows`, `test_link_sql_uses_link_key_both_ways` | Diagnostic shape query; recall↔enforcement on `link_key` in both directions |
| `test_slices_include_overall_and_unknown_year` (4), `test_slice_columns_exist_in_prod` (4), `test_top_n_and_tiers` | Slices: overall row, `unknown` year, PROD columns only, top-N and tiers |
| `test_completeness_covers_every_business_column` (4), `test_completeness_treats_blank_strings_and_empty_arrays_as_missing` | Completeness covers `FLAT_COLUMNS`; blanks and empty arrays count as missing |
| `test_wilson`, `test_score_*` (3), `test_four_fifths_is_not_applied_to_error_rates`, `test_maude_rows_split_into_three_metrics`, `test_completeness_skips_the_slice_own_column` | Wilson CI, gap and flag rules, small cells, metric split |
| `test_run_writes_csvs_and_summary`, `test_run_stops_above_max_gb_before_any_real_query`, `test_estimate_only_runs_no_real_query`, `test_empty_result_fails_loudly`, `test_print_only_makes_no_gcp_call` | CSVs and `summary.json`; cost guard; `maximum_bytes_billed`; fails loudly on zero rows |

## Manual checks (real BigQuery, not in pytest)

These need real BigQuery, so they are not tests (no real GCP in pytest). They were run by hand on 2026-10-25 with the user's approval. Re-run them after any SQL change.

| Check | Result on 2026-10-25 |
|---|---|
| Dry run of the RAW load script and each statement (`load_raw_to_bq.py`) | Pass. A DELETE without the `_ingest_date` filter is rejected, as intended |
| Dry run of the 4 staging scripts (`build_staging.py`) | Pass |
| Dry run of bounds, MERGE, rebuild and date-stability SQL (`merge_prod.py`) | Pass after a fix (GUDID typed NULL) |
| Parity: `norm_date` / `norm_mfr` vs `normalize_date` / `normalize_manufacturer`, on inline values (0 bytes read) | Match on 23 of 24 values. The impossible date `20241399` gives NULL in SQL (intended). The suffix bugs are copied on purpose (`docs/issue.md` #13) |
| Parity: each staging SELECT vs `flatten_*` on inline payloads | Enforcement, recall and MAUDE: 0 cell differences. GUDID: matches hand-written expectations |
| GUDID fetch-time conversion on a real GCS sample (`samples/gudid/gudid_sample_20261001.zip`) | T1–T4 pass after the #12 fix |

## Known open issues

1. **Null-typed Parquet column (unverified here):** on test-cases data, `gudid_flat.parquet` had `publish_date` typed `null`, which BigQuery probably can't load. If it recurs, the fix belongs in `flatten_to_parquet.py`: cast all-null columns to an explicit type.
2. **Relative path in data-quality tests:** `test_data_quality.py` reads `data/interim` relative to the working directory, so it must run from the repo root.

## Not covered

- Real BigQuery behavior, and real GCS outside the opt-in integration file (deliberately avoided; see the mocking note at the top). The warehouse SQL is checked by hand instead (see "Manual checks").
- End-to-end DAG runs of `vigil_md_acquisition` and `vigil_md_full_load` (`dag.test()` would run the real scripts).
- `flatten_to_parquet.py` is covered only through its helper functions in `test_preprocessing.py`.
- `pull_samples.py` and `exact_linking.py` have no unit tests yet.
