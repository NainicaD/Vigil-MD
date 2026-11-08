# Model-Development

Links MAUDE adverse-event device records to GUDID devices, aiming for 99% precision or better on
the records it calls a match. The data pipeline lives in `../Data-Pipeline/`; this folder only
reads its BigQuery output (`vigil_md_prod`) and never writes to it.

New here? Read `docs/SAME_DEVICE_DEFINITION.md` for what "same device" means, then
`docs/TABLES.md` for which table is which.

## Setup

Python 3.12 or newer. From this folder:

```bash
python -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -r requirements.txt       # SQL stages and tests
pip install -r requirements-ml.txt    # also needed for s04-s09 (pandas, sklearn, xgboost, mlflow)
```

For the stages that read BigQuery you also need credentials and the project id:

```bash
gcloud auth application-default login
export PROJECT=<your-gcp-project-id>   # the id, e.g. from `gcloud config get-value project`
```

Every command below assumes the venv is active and `$PROJECT` is set. Scripts resolve their own
paths, so they run from any directory.

## Layout

Two folders hold the code. `vigil_model/` is the library: the rules, imported everywhere and
never run directly. `pipeline/` is the entry points, numbered in the order they run.

| Path | What it holds |
|---|---|
| `vigil_model/same_device_features.py` | **The rules.** `pair_features`, the key and placeholder rules, the SQL macros. The only place a rule may be written |
| `vigil_model/bq_utils.py` | Read-only guards, dry-run cost estimate, SQL loading and macro expansion |
| `vigil_model/rows.py` | Table cell -> `DeviceSide` values, shared by `s04` and `s05` so the two feature tables cannot drift |
| `vigil_model/golden_fixture.py` | Proves Python and BigQuery apply the same rules |
| `pipeline/s01_build_label_set.py` | Runs `sql/labels/*.sql`, writes the label tables to `vigil_md_model` |
| `pipeline/s02_build_hand_label_sheet.py` | Draws the hand-label sample, writes `labels/hand_label_sample_v1.csv` |
| `pipeline/s03_export_label_set.py` | Downloads `label_set_v1` locally. Free: uses the row-listing API, not a query |
| `pipeline/s04_build_feature_table.py` | **`label_set_v1` -> the `feat_*` training table.** The only training stage that needs a billed query |
| `pipeline/s05_build_hand_features.py` | The same features for the hand-label sheet |
| `pipeline/s06_train_models.py` | Logistic regression + 3 XGBoost variants; cutoff picked on validation, then scored on the test split and the hand sheet |
| `pipeline/s07_rules_baseline.py` | The guide's decision tree, no training. The bar a model must beat |
| `pipeline/s08_evaluate_hand.py` | Precision and recall on the hand labels, weighted, with a 95% lower bound |
| `pipeline/s09_evaluate.py` | The generic scorer, for any labels + scores pair |
| `pipeline/` (unnumbered) | Side analyses the training chain does not depend on |
| `sql/`, `labels/`, `reports/`, `docs/` | Queries, committed label data, generated results, the written rules |
| `data/` | Local outputs: parquet, features, scores. Git-ignored, regenerable |
| `tests/` | Mocked tests; no test talks to real GCP |

## The training chain

```bash
python pipeline/s03_export_label_set.py --project $PROJECT      # free, no query cost
python pipeline/s04_build_feature_table.py --project $PROJECT   # ~0.3 GB, cached afterwards
python pipeline/s05_build_hand_features.py
python pipeline/s06_train_models.py
```

`s06` prints the `s08_evaluate_hand.py` command for each model, with the cutoff it chose.

Stages `s01` and `s02` rebuild the label set and the hand-label sheet. They are expensive and
rarely needed; only run them if you know why. `docs/LABELS.md` says what they do and
`docs/TABLES.md` gives the cost of each stage.

## Before any query, every time

Each BigQuery stage takes the same three flags. Use them in this order:

```bash
python pipeline/<stage>.py --project $PROJECT --print-only      # show the SQL, no GCP call
python pipeline/<stage>.py --project $PROJECT --estimate-only   # free dry run, prints GB
python pipeline/<stage>.py --project $PROJECT                   # the real run
```

The free tier is 1 TiB of scan per month for the whole team, so check the estimate before you
spend. `--max-gb` refuses to run anything above it. Exit codes: 0 ok, 2 broken SQL or config,
3 over budget.

Guards that cannot be bypassed: a read query must be a single `SELECT`/`WITH` against allowed
datasets with no write keywords; a write must be exactly one `CREATE OR REPLACE TABLE` into
`vigil_md_model`. Anything else is refused before it reaches BigQuery.

## Adding a query

1. Create `sql/<area>/NN_short_name.sql`.
2. Start it with the header lines `-- Title:`, `-- Check:`, `-- Why:` and `-- Informs:`.
3. Use `__MAUDE__` and `__GUDID__` for the tables. They are rendered to
   `<project>.vigil_md_prod.{maude,gudid}`.
4. Write rules as `{{ macro() }}`, never as a regex literal. The macros come from
   `vigil_model/same_device_features.py`, so Python and SQL cannot disagree. A test fails if a
   regex literal appears in `sql/labels/`.
5. To get several result tables from one file, start each query with a `-- @part name` line.
6. Run the tests. They check every file's header, rendering and read-only status.

## Tests

```bash
python -m pytest        # from this folder; none of them talk to real GCP
```

Imports resolve through `pythonpath = ["."]` in `pyproject.toml`, so tests and scripts import
`vigil_model.*` and `pipeline.*` without any `sys.path` juggling. Tests are mocked by policy: a
real run would pollute the project's logs and spend the shared query budget.

## Shared conventions

- **Model key:** `UPPER(REGEXP_REPLACE(TRIM(model), r'[\s\-_./]', ''))`.
- **Placeholder rule:** a model key is a placeholder (treated as missing) if it is under 2
  characters or matches
  `^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$`.
- **One GUDID record per DI:** where needed, take the newest `publish_date`, with ties broken by
  `source_record_id`.
- **Column namespaces:** `key_*` are raw join and audit inputs, `feat_*` are the only columns a
  model may see, `meta_*` are split and weighting fields. A UDI must never reach a `feat_*` value;
  `assert_no_leakage` and `assert_no_value_leakage` enforce it.

Both the Python feature function and the SQL use the rules above, so training and serving see the
same values.
