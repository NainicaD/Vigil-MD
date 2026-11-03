# Vigil-MD Tests Plan: warehouse scripts and DAGs

## Context

`docs/WAREHOUSE_PLAN.md` Steps 0–9 added new code with **no tests in `tests/`**. Each step was checked only by throwaway scripts in a scratchpad, plus BigQuery dry runs. This plan turns those checks into a permanent, mocked pytest suite.

New code to cover:

| File | Step |
|---|---|
| `scripts/warehouse/schemas.py`, `create_tables.py` | 1 |
| `scripts/acquisition/fetch_sources.py` (incremental mode) | 2 |
| `scripts/warehouse/convert.py`, `common.py`, `unzip_to_ndjson.py` | 3 |
| `scripts/acquisition/gudid_convert.py` | 3 (issue #11) |
| `scripts/warehouse/load_raw_to_bq.py` | 4 |
| `scripts/warehouse/build_staging.py`, `sql/staging_*.sql` | 5 |
| `scripts/warehouse/merge_prod.py` | 6 |
| `dags/vigil_md_acquisition.py` (warehouse wiring) | 7 |
| `dags/vigil_md_full_load.py` | 8 |

**Baseline on 2026-10-25** (`~/.venvs/vigil-airflow/bin/python -m pytest tests --ignore=tests/test_data_quality.py --ignore=tests/test_preprocessing.py`): 55 passed, 50 skipped (no local `data/interim`), 21 deselected (integration). The new code did not break any existing test.

## Decisions

| # | Decision | Status |
|---|---|---|
| TD1 | **No real GCP in any test.** GCS and BigQuery are mocked (`fake_gcp` in `tests/conftest.py`, or the fakes below). Real-run failures pollute the project's logging. | Locked (user rule) |
| TD2 | **Logstash stays out of tests.** An autouse fixture in `tests/conftest.py` makes `scripts.logger.LogstashHandler.emit` a no-op. Before it, any test that logs could reach Logstash on `localhost:5044` if it was running. | **Done** (T1) |
| TD3 | **`ijson` in the test venv.** The openFDA conversion needs `ijson`. It is in `requirements.txt`, but `~/.venvs/vigil-airflow` did not have it. | **Done:** installed 3.6.0 (`~/.local/bin/uv pip install --python ~/.venvs/vigil-airflow/bin/python ijson`) |
| TD4 | **The SQL-vs-Python parity check stays manual.** It needs real BigQuery (TD1). `TEST_CASES.md` lists it, and the BigQuery dry runs, under "Manual checks" (Step T7). Tests check the SQL **text** (columns, filters, statement order), not its result. | Locked (follows TD1) |
| TD5 | **Fakes model the real libraries.** In Step 3 a fake bucket hid a real bug (`docs/issue.md` #12). The shared `FakeBucket` must model: `open("wb")` terminates on an exception, `open("wt")` finalizes even on an exception, `rename_blob`, `copy_blob`, `exists`, `delete`, `metadata` + `patch`. | Locked |
| TD6 | Tests go in `tests/` (and `tests/dags/` for DAG tests), one test file per source module, the same layout as today. | Locked |

---

## GUARDRAILS FOR THE IMPLEMENTING AGENT (read before every step)

### Stop rules
- T-G1. Do **one step at a time**. When a step is complete, write the Step Report (template at the end), tick its boxes in this file, and **STOP**.
- T-G2. Do not start step N+1 until the user writes **"approved test step N"**.
- T-G3. If a step needs a file that is not in its "Allowed files" list, STOP and ask.
- T-G4. **Do not change production code to make a test pass.** If a test finds a bug, STOP. Report the bug with file:line, and mark the test `xfail(strict=True, reason="docs/issue.md #N")` only after the user agrees. Log the bug in `docs/issue.md`.

### No real cloud, network or Logstash
- T-G5. No test may create a real `google.cloud` client, open a socket, or call the FDA or AccessGUDID. Use `fake_gcp`, the shared fakes, and `monkeypatch`.
- T-G6. Do not run `pytest -m integration`. Do not edit `tests/test_raw_data_integration.py`.
- T-G7. The Logstash guard fixture (TD2) is autouse. Do not remove it.

### Test style (match the existing suite)
- T-G8. Plain pytest functions, fixtures and `monkeypatch`. Section rulers `# ── Name ──`. A short module docstring that says what is mocked. Follow `tests/test_warehouse_load_mocked.py`.
- T-G9. One behaviour per test, with a name that states it (`test_same_fingerprint_copies_without_download`).
- T-G10. Prefer the pure functions (`classify_partition`, `render_*_sql`, `convert_zip`, `pick_latest`, …). Use a `main()` test only for orchestration (which manifests are read or deleted, which errors propagate).
- T-G11. SQL tests check structure with regex and `str.index` order. They do not compare whole SQL strings, so formatting changes do not break them.
- T-G12. Tests run from the repo root and must not write into the repo (`tmp_path` only). The DAG tests keep the rule of `test_parsing_has_no_side_effects`.
- T-G13. Do not reformat or edit existing tests. The only allowed edits to existing files are the ones named in a step.
- T-G14. No git commit or push unless the user asks.

---

## Shared fakes (Step T1)

Add to `tests/conftest.py`, after the existing `fake_gcp`:

| Fake | Models |
|---|---|
| `FakeBucket` / `FakeBlob` | in-memory `store: dict[name, bytes]`; `exists`, `upload_from_filename`, `upload_from_string`, `download_as_text`, `delete`, `open("wb"/"wt"/"rb")` with the real exit behaviour (TD5), `copy_blob`, `rename_blob`, `metadata` + `patch`, `client.list_blobs(bucket, prefix, max_results)` |
| `FakeSession` | `get(url, stream=…, timeout=…)` returning a context-manager response with `iter_content`, `content`, `json()`, `raise_for_status`; records calls; per-URL payloads |
| `make_openfda_zip(records)` | an in-memory zip with one `{"meta", "results"}` JSON member |
| `make_gudid_zip(parts)` | an in-memory zip of `FULLDownload_Part{n}_Of_{N}_{date}.xml` parts with a correct header (shape from the real Part1 sample) |

---

## STEP CHECKLIST

### ☑ Step T1: Shared fakes and the Logstash guard
- **Allowed files:** `tests/conftest.py` (add only), `tests/test_fakes.py` (new).
- [x] Add the autouse Logstash guard (TD2).
- [x] Add `FakeBucket`, `FakeBlob`, `FakeSession`, `make_openfda_zip`, `make_gudid_zip` (as fixtures or plain helpers). *(Also `gudid_device` for one `<device>` element.)*
- [x] `tests/test_fakes.py`: test the fakes themselves. `"wb"` + an exception leaves no object. `"wt"` + an exception leaves a finalized object. `rename_blob` moves bytes. `list_blobs` filters by prefix.
- [x] **Verify:** the full suite still gives 55 passed + the new tests, the same skips, and no new failures. *(68 passed = 55 + 13, 50 skipped, 21 deselected.)*
- [x] Report, then STOP.

### ☑ Step T2: Conversion (`convert.py`, `gudid_convert.py`)
- **Allowed files:** `tests/test_convert.py`, `tests/test_gudid_convert.py` (new).
- [x] `convert.py`:
  - openFDA: payload equals the source records; floats stay floats (`use_float`); 0 records raises; 0 or 2 `.json` members raise.
  - GUDID `xml_to_value`: namespace stripped; `xsi:nil` → null; leaf text kept exactly; plural wrapper → always a list; other repeated tags → list; attributes → `@name`.
  - `gudid_parts`: part order 1..N; a missing part raises; mixed `Of_N` totals raise.
  - Counts: part count ≠ header raises; total ≠ `numberRecordsDatabase` raises.
  - Hash: stable across meta, key order, member name; changes when one record changes.
  - `Utf8Writer` writes UTF-8 bytes and has no `close`.
- [x] `gudid_convert.py`, with `FakeBucket`:
  - `process_local_zip`: `load` (zip + NDJSON + metadata), `unchanged_hash` (server-side copy, no NDJSON), `unchanged_content` (NDJSON removed), conversion error (raises, **no NDJSON object**, regression for issue #12).
  - `convert_local_zip` opens the NDJSON blob with mode `"wb"` (regression for #12).
  - `fetch_and_convert` with `FakeSession`: downloads once to a temp file; the temp file is gone afterwards.
- [x] Report, then STOP.

> **T2 notes:** TD3 resolved: `ijson` 3.6.0 installed in `~/.venvs/vigil-airflow` with `uv pip install` (the venv has no pip). Mutation check: putting back the old `blob.open("wt")` makes both #12 regression tests fail, as intended.

### ☑ Step T3: Incremental fetch (`fetch_sources.py`)
- **Allowed files:** `tests/test_fetch_incremental.py` (new).
- [x] `classify_partition`: new, changed, same, nulls on either side, GUDID fingerprint.
- [x] `openfda_partition_key`.
- [x] `fetch_partition_incremental` + `record_incremental_action`: copied (0 downloads), already_present, unchanged_hash (nothing pending, `_incoming` cleaned), load (pending written, state untouched), dry_run (no writes).
- [x] `fetch_gudid` incremental: new snapshot → complete pending entry (with `content_sha256`, `ndjson_path`); same snapshot → no download; new bytes, same records → `unchanged_content`, state `zip_sha256` refreshed; identical bytes → `unchanged_hash`; dry run; full mode streams the zip and does not convert.
- [x] `require_partition_state` raises with "Run vigil_md_full_load first"; `save_pending` writes nothing for an empty list.
- [x] `main()` with `fake_gcp` + `FakeSession`: incremental mode checks every selected source's state **before** any download; `--mode full` never reads partition state; `manifest_state.json` keeps all its keys (G13 of the warehouse plan).
- [x] Report, then STOP.

### ☑ Step T4: Unzip and raw load (`common.py`, `unzip_to_ndjson.py`, `load_raw_to_bq.py`)
- **Allowed files:** `tests/test_unzip_to_ndjson.py`, `tests/test_load_raw_to_bq.py` (new).
- [x] `pick_latest`: newest export folder only; non-zip ignored; GUDID fixed key; 2 GUDID zips in one folder raise; empty → `[]`.
- [x] `apply_content_check`: new / changed / unchanged → `skip_load`.
- [x] `unzip` `main()`: `--scope changed` skips entries that already have `content_sha256` (GUDID from the fetch); `--scope all` writes a `full_*.json` manifest; the manifest is saved after each file; `--print-only` imports no `google.cloud`.
- [x] `load_raw_to_bq`:
  - SQL: the DELETE has `_ingest_date = @ingest_date AND _source_file = @source_file`; RAISE comes before BEGIN; order DELETE → INSERT raw → INSERT load_log → COMMIT; every `@param` in the SQL is bound by `query_params`.
  - `manifest_in_scope` (daily vs `full_`), `check_entry` (not unzipped, 0 rows, `skip_load` passes), `tmp_table_name` (always `vigil_md_ops`).
  - `main()` with `fake_gcp`: a new file loads; an already-loaded file is skipped and its state refreshed; `skip_load` is not loaded; the daily manifest is deleted and `full_` is untouched; a load error keeps the manifest; the temp table is dropped in `finally`, including on error.
- [x] Report, then STOP.

### ☑ Step T5: Staging, merge, schemas
- **Allowed files:** `tests/test_build_staging.py`, `tests/test_merge_prod.py`, `tests/test_schemas.py` (new).
- [x] `schemas`: 13 table specs; every merge key, cluster field and partition field is a column of its table; the protected guard blocks `vigil_md_processed.*` and `*_sample`; GUDID merge key is `source_record_id`.
- [x] `build_staging`: each `staging_*.sql` passes `validate_source_sql`; the validator rejects a missing partition filter and a missing column; the INSERT column list equals the `schemas.py` staging columns; dedup is `PARTITION BY <keys> ORDER BY _loaded_at DESC, record_hash`; `DECLARE` is first; a raw partition with 0 rows exits 0 with no script run (`fake_gcp`).
- [x] `merge_prod`:
  - ON clause: keys only for GUDID; keys + `_part_date BETWEEN @min_d AND @max_d OR _part_date = sentinel` for the others.
  - INSERT columns = prod table columns; UPDATE never sets `first_loaded_at`; no `NOT MATCHED BY SOURCE`, no DELETE in the daily MERGE; BEGIN → MERGE → dup check → ROLLBACK/RAISE → COMMIT.
  - Rebuild: same columns, `_ingest_date >= DATE '1900-01-01'`, one transaction, no `CREATE OR REPLACE`.
  - Bounds SQL for GUDID uses `CAST(NULL AS DATE)` (regression for the dry-run bug).
  - CLI: `--rebuild` without `--yes-rebuild` exits 2 with no client; `--ingest-date` required unless `--rebuild`; an empty staging partition exits 0 with no MERGE (`fake_gcp`).
- [x] Report, then STOP.

> **T5 additions:** the staging `record_hash` covers the business columns only; MAUDE keeps reports with an empty `device[]`; the MERGE gets the staging date range as `@min_d` / `@max_d`; all-NULL dates match only the sentinel; GUDID gets no date parameters; `create_tables.py --print-only` makes no GCP call.

### ☑ Step T6: DAG tests
- **Allowed files:** `tests/dags/test_warehouse_dags.py` (new), `tests/dags/conftest.py` (add fixtures only).
- [x] Both DAGs parse; dag_ids unique; parsing creates no files (reuse the existing DagBag fixture pattern). *(Uniqueness and no-side-effects are already covered for the whole `dags/` folder by `tests/dags/test_acquisition_dag.py`, so they are not repeated.)*
- [x] `vigil_md_acquisition`: schedule `0 6 * * *`, `catchup=False`, `max_active_runs=1`; the graph from Step 7; trigger rules (`validate_gcs` and `warehouse_start` `NONE_FAILED_MIN_ONE_SUCCESS`, alert `ONE_FAILED`); all warehouse tasks in pool `vigil_warehouse`.
- [x] `check_manifest` with faked `requests` and storage: only changed sources + `warehouse_start`; pending only → `warehouse_start`; nothing → `no_update`.
- [x] `warehouse_args` and `full_load_args`: each argument list is accepted by its script's argparse (`--print-only`, subprocess, no GCP).
- [x] `vigil_md_full_load`: `schedule=None`, params and defaults; guard (0 runs passes, 1 run raises); refetch false → no fetch, true → `--mode full` per selected source; an unselected source → `AirflowSkipException`; rebuild flags only on merge.
- [x] Do **not** use `dag.test()` on these two DAGs (their tasks call real scripts).
- [x] Report, then STOP.

> **T6 note:** `tests/dags/conftest.py` needed no change. The existing isolated Airflow home and SQLite DB are enough.

### ☑ Step T7: Documentation and the full run
- **Allowed files:** `tests/TEST_CASES.md`, `docs/WAREHOUSE_PLAN.md` (tick "Out of scope → tests" only).
- [x] Add the new files and their cases to `tests/TEST_CASES.md` (summary table + one section per file). Add a "Manual checks" section for the BigQuery dry runs and the parity check (TD4).
- [x] Run the full suite. Report the counts against the baseline (55 passed, 50 skipped, 21 deselected). *(283 passed, 50 skipped, 21 deselected: +228 tests, no new skips, no failures.)*
- [x] Report, then STOP.

### Out of scope
- `tests/test_warehouse_load_mocked.py`: the old loader (`load_interim_to_bq.py`) did not change, so this file needs no change. (The warehouse plan listed it as a possible change.)
- Fixing open issues (`docs/issue.md` #2, #5, #6, #7, #8, #9, #10, #13). Tests may document them with `xfail` only after the user agrees (T-G4).
- Real-BigQuery tests (TD1).

## Step Report template

```
Test step N report
- Files created / changed: <path>
- Tests added: <count>, all passing: <yes/no>
- Full suite: <passed> passed, <skipped> skipped, <deselected> deselected (baseline 55/50/21)
- Bugs found in production code: <none | file:line + docs/issue.md #>
- Guardrails I was close to: <none | T-Gx and why>
- Open questions for the user: <list or none>
- Waiting for: "approved test step N"
```
