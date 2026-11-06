# Model-Development

Work for the 3-day model development plan: linking MAUDE adverse-event device records to GUDID
devices at 99% precision or better. The data pipeline lives in `../Data-Pipeline/`; this folder
only reads its BigQuery output (`vigil_md_prod`) and never writes to it.

## Layout

| Path | What it holds |
|---|---|
| `docs/SAME_DEVICE_DEFINITION.md` | The definition of "same device", edge cases, label rules |
| `sql/same_device/NN_*.sql` | One profiling query per file. Each starts with a `Title / Check / Why / Informs` header |
| `scripts/same_device_profile.py` | Runs the `sql/same_device` queries, writes `reports/same_device/` |
| `scripts/bq_utils.py` | Read-only guard, dry-run cost estimate, SQL loading (copied from `Data-Pipeline/scripts/analysis/bias_slices.py`) |
| `reports/same_device/` | Query results as CSV, plus `RESULTS.md` (generated: query, check, result) |
| `tests/` | Mocked tests; no test talks to real GCP |

## Running the same-device profile

From WSL, in this folder. The `vigil-analysis` venv has `google-cloud-bigquery`, and auth comes
from `gcloud auth application-default login`.

```bash
PY=~/.venvs/vigil-analysis/bin/python
$PY scripts/same_device_profile.py --project vigil-md-1792696816 --print-only     # show the SQL
$PY scripts/same_device_profile.py --project vigil-md-1792696816 --estimate-only  # free dry run, GB per query
$PY scripts/same_device_profile.py --project vigil-md-1792696816                  # run all, write reports/same_device
$PY scripts/same_device_profile.py --project vigil-md-1792696816 --only 05 07     # re-run a subset
```

Safety:
- Every query must be a single `SELECT`/`WITH`, read only from `vigil_md_prod`, and contain no
  write keywords. Otherwise the script refuses to run it.
- All parts are dry-run first. If any part fails to compile, the script exits 2 and nothing
  runs. If any part is above `--max-gb` (default 5), the script exits 3 and nothing runs.
- Each job also sets `maximum_bytes_billed`. A full run scans about 19 GB.

## Adding a query

1. Create `sql/same_device/NN_short_name.sql`.
2. Start it with the header lines `-- Title:`, `-- Check:`, `-- Why:` and `-- Informs:`.
3. Use `__MAUDE__` and `__GUDID__` for the tables. They are rendered to
   `vigil-md-1792696816.vigil_md_prod.{maude,gudid}`.
4. To get several result tables from one file, start each query with a `-- @part name` line.
   Each part is written to its own `NN_short_name__name.csv`.
5. Run the tests. They check every file's header, rendering and read-only status.

## Tests

```bash
~/.venvs/vigil-airflow/bin/python -m pytest      # from this folder
```

## Shared conventions

- **Model key:** `UPPER(REGEXP_REPLACE(TRIM(model), r'[\s\-_./]', ''))`.
- **Placeholder rule:** a model key is a placeholder (treated as missing) if it is under 2
  characters or matches
  `^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$`.
- **One GUDID record per DI:** where needed, take the newest `publish_date`, with ties broken by
  `source_record_id`.

The shared feature function (Day 1) must use the model-key and placeholder rules above, so that
training and serving see the same values.
