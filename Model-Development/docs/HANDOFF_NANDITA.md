# Handoff to Nandita: Day 1 training-label track

From Hitika, 7 Nov. H1 to H3 of the Day 1 plan are written and tested offline (232 tests pass). **Nobody has run any SQL on BigQuery.** The BigQuery side is yours: the tables below have to exist before anything runs.

## What is done (Hitika)

| Item | File |
|---|---|
| Shared feature module (bodies, leak guards, SQL emitters) | `scripts/same_device_features.py` |
| Label SQL, steps 02 to 06 | `sql/labels/02_positives.sql` to `06_label_stats.sql` |
| Build script | `scripts/build_labels.py` |
| Tests | `tests/test_same_device_features.py`, `tests/test_label_sql.py`, `tests/test_build_labels.py` |

Not done yet: the golden fixture (H4, needs one BigQuery hand-run and a `--fixture` mode) and the pilot labeling (H5, waits for your guide and pilot cut).

## What you build

### 1. Dataset and write guard (N1)
- Create `vigil_md_model`.
- `build_labels.py` has a **local copy** of the write guard and the macro pass, so it runs without your changes. When yours land in `bq_utils.py`, swap them in and delete mine. Only one of us edits `bq_utils.py`: you.
- Your `render()` must handle a macro with **no arguments**: `{{ suffix_lens() }}`.

### 2. `sql/labels/00_device_split.sql` → `vigil_md_model.device_split` (N2)
The label SQL reads exactly these columns:

| Column | Meaning |
|---|---|
| `base_key` | `{{ device_key(manufacturer_name_norm, model_number_raw, catalog_number) }}` for each GUDID record. This is labeler + model key with **no** stem merge. |
| `device_key` | The stemmed key: your suffix-sibling merge of `base_key`, 2 levels at most. |
| `split` | `{{ split(device_key) }}` computed on the **stemmed** key. Hand-labeled devices (your override table) are always `test`. |

Rules that break things if missed:
- **One row per `base_key`.** A duplicate duplicates label rows.
- **Cover every GUDID `base_key`.** `05_label_set.sql` stops with an error if any row has no split.
- `device_key` in the module is the unstemmed base key, because its signature takes one record. The stem merge lives only in your SQL.
- Use `{{ suffix_lens() }}` for the 1 to 3 suffix lengths. Write no regex or normalization literals. A test fails if one appears in `sql/labels/`.
- `validate_labels.py` (C6) recomputes `split_of(meta_device_key)`, so `split` must match that hash of the stored key exactly.

### 3. `sql/labels/01_alias_table.sql` → `vigil_md_model.alias_table` (N3)
- Columns `maude_mfr` and `gudid_mfr` are required. Extra columns, such as a DI count, are allowed.
- Train-split devices only, and at least 3 distinct DIs per pair (`ALIAS_MIN_DIS` in the module).
- It must not be empty.
- **Export it with `build_labels.py --export-alias`**, not by hand. That writes `labels/alias_table_v1.csv` and the hash in the manifest. Any other export will not match, and the gate will refuse to build.

### 4. Validator (N4), guide, pilot cut (N5)
Column names in `vigil_md_model.label_set_v1`:

| Check | Columns |
|---|---|
| C1 recompute `pair_id` | `pair_id` = `pair_id(key_mdr_report_key, key_device_seq, key_gudid_udi_di, label_source)` |
| C2, C6 | `meta_device_key`, `meta_split` |
| C3 | `label` (`MATCH` or `NON_MATCH` only in this table) |
| C5 | No `feat_*` column is written here. Check `FEATURE_COLUMNS` and that `key_gudid_udi_di` is not in it. |
| C7 | `key_m_mfr`, `key_m_model`, `key_m_cat`, `key_m_pc`, `key_gudid_udi_di` |
| C4 | Small tables `label_stats_fill` (per split and side, same fields and definition as `02_fill_rates.csv`) and `label_stats_years` (rows per split and year, for the baseline weights) |

Other columns: `label_source` (`udi_link`, `suffix_sibling`, `same_labeler_hard`, `random_labeler`), `key_match_kind`, `meta_year`, `meta_n_dup_rows`, `meta_n_dis_for_model`.

The guide and the pilot cut (N5a, N5c) do not depend on any of this. Hand labeling can start as soon as they exist.

## Run order (one person, with BigQuery write access)

```bash
cd Model-Development
PY=~/.venvs/vigil-analysis/bin/python
$PY scripts/build_labels.py --project vigil-md-1792696816 --print-only            # no GCP call
$PY scripts/build_labels.py --project vigil-md-1792696816 --only 00 01 --estimate-only
$PY scripts/build_labels.py --project vigil-md-1792696816 --only 00 01 --snapshot-date 2026-11-06
$PY scripts/build_labels.py --project vigil-md-1792696816 --export-alias          # then commit labels/
$PY scripts/build_labels.py --project vigil-md-1792696816 --estimate-only         # 02 to 06
$PY scripts/build_labels.py --project vigil-md-1792696816 --snapshot-date 2026-11-06
```

- Steps 02 and later **refuse to run** unless `alias_table` in BigQuery hashes to the value in `labels/label_set_v1.manifest.json`. Run `--export-alias` after step 01 and before step 02.
- `--max-gb` is 25 per part. A part over it exits 3. Exit 2 means broken SQL or config.
- Parts that read tables built earlier in the same run are estimated just before they run, so `--estimate-only` cannot price them.
- Tests: `~/.venvs/vigil-airflow/bin/python -m pytest -q --ignore=tests/test_embedding.py`

## Do not do

- Do not write regex or normalization rules anywhere. They live in `same_device_features.py`. If you need a new macro, ask Hitika.
- Do not relax `assert_read_only`.
- Do not run anything for Claude. Claude runs only `--print-only` and pytest.

## Known risks and open points

- **First real run may fail.** The SQL was never executed. Expect small BigQuery errors in 02 to 04.
- **Easy negatives:** the plan says both "~10%" and "0.2 easy". I set `--easy-anchor-pct 10`. The real share comes out lower, because some draws are filtered out.
- **Shortcut risk:** positives never have match kind `conflict`, so `conflict` always means NON_MATCH in this set. A model that sees `feat_match_kind` will learn that. Part of why UDI-split numbers are optimistic.
- **Token match:** `contains_as_token` matches one whitespace-delimited word only ("IP 200" split across words is missed).
- **Kit words:** the regex is copied from query 13.
- Training numbers are an optimistic upper bound for the no-UDI pool. Put that in `LABELS.md` before anyone reports a metric.
