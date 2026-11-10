# Same-device labeling: technical status

As of 7 Nov 2026, branch `same-device-profiling` (pushed, 10 commits ahead of `main`). Written for team members who
build on this work: Nandita (BigQuery side, hand labels), Satwik (features, XGBoost, MLflow) and Hitika (shared
module, label build, hand labeling). Companion documents: [`LABELS.md`](LABELS.md) (what the labels are and their
limits), [`SAME_DEVICE_DEFINITION.md`](SAME_DEVICE_DEFINITION.md) (the rules E1 to E11),
[`HAND_LABEL_GUIDE.md`](HAND_LABEL_GUIDE.md), [`TABLES.md`](TABLES.md).

## 1. Goal and approach

Decide whether a MAUDE report row and a GUDID device record describe the same device, mainly for the MAUDE rows that
carry no UDI. The approach has three parts:

1. **One definition of the rules**, written once in Python and emitted into SQL as macros. No second copy anywhere.
2. **Rule-built training labels** from MAUDE rows whose UDI links to GUDID, plus negatives built from near and far
   devices. Split by device group so no device is on both sides.
3. **A small hand-labeled test set** of no-UDI rows, sampled in strata, to measure what the rule labels cannot.

```
 vigil_md_prod.maude ─┐                                    ┌─> training (feat_* columns, train split)
                      ├─> build_labels.py (SQL 00..06) ───>│
 vigil_md_prod.gudid ─┘      label_set_v1 (BigQuery)        └─> checks and first validation
        │                          ▲
        │       same_device_features.py (rules: Python + SQL macros)
        │                          ▲
        └─> make_hand_label_sheet.py ──> hand_label_sample_v1.csv ──> hand labels ──> honest test
```

## 2. Status by item

| Item | Owner | State |
|---|---|---|
| H1 shared feature module | Hitika | Done. Bodies, leak guards, SQL emitters. |
| H2 label SQL (02 to 06) | Hitika | Done and run on BigQuery. |
| H3 `build_labels.py` | Hitika | Done and run. Dry run, byte cap, alias gate, manifest. |
| H4 golden fixture | Hitika | Done. 45 cases, 0 differences on BigQuery (7 Nov). |
| H5 pilot hand labeling | Hitika | **In progress.** Sheet merged, 0 of 352 lines labeled. |
| N1 dataset and write guard | Nandita | Done (`bq_utils.py`: macro pass, write guard). |
| N2 `device_split` | Nandita | Done. In BigQuery. `n_bad_split` = 0. |
| N3 `alias_table` | Nandita | Done. 1,756 pairs, hash in the manifest. |
| N4 label validator (checks C1 to C7) | Nandita | **Not in the repo.** Nandita to confirm. |
| N5 hand-label sheet and pilot cut | Nandita | Done. 150 MAUDE rows, 352 lines. |
| N6 hand-label guide and agreement script | Nandita | Done (`hand_label_agreement.py`). |
| Feature code, XGBoost, evaluation, MLflow | Satwik | **In progress** on `model-dev/*` branches. See section 6. |

## 3. What is built

| Area | Files |
|---|---|
| Rules (only copy) | `vigil_model/same_device_features.py` |
| Label build | `scripts/s01_build_label_set.py`, `sql/labels/00` to `06`, `labels/label_set_v1.manifest.json`, `labels/alias_table_v1.csv` |
| Python and BigQuery parity | `vigil_model/golden_fixture.py`, `tests/fixtures/normalization_golden.csv`, `s01_build_label_set.py --fixture` |
| Hand labels | `scripts/s02_build_hand_label_sheet.py`, `sql/hand_labels/01` to `03`, `labels/hand_label_sample_v1.csv` and manifest, `vigil_model/hand_label_agreement.py` |
| Tests | 352 offline tests (`tests/`), no GCP calls |

### 3.1 Shared module (`same_device_features.py`)
- **Normalization:** `identifier_key` is `UPPER` with `[\s\-_./]` removed. `is_placeholder` marks keys under 2 characters
  or in `PLACEHOLDER_RE`. `usable_key` is the key unless it is a placeholder.
- **Match kind:** `identifier_match` follows the CASE of query 08 line by line. The order is `same_model_key`,
  `maude_model_eq_gudid_catalog`, `maude_catalog_eq_gudid_model`, `catalog_eq_catalog`,
  `maude_model_missing_or_placeholder`, `gudid_model_missing`, `prefix_related`, `contains`, `conflict`.
- **Features:** `pair_features` returns the 19 `FEATURE_COLUMNS` (`feat_*`). Only those columns may reach a model.
- **Leak guards:** `assert_no_leakage` rejects column names with banned whole tokens (`udi`, `di`, `pair_id`,
  `label`, `split`, `device_key`). `assert_no_value_leakage` rejects a `feat_*` value that contains the DI or is a 6+
  character piece of it.
- **Identity:** `device_key` is `labeler|model key` (catalog key if the model is unusable) and is not stem-merged.
  `split_of` is `SHA-256("v1|key")`, test when the first 8 hex digits mod 100 are under 20. `pair_id` is the first 32
  hex characters of `SHA-256("mdr|device_seq|di|role")`, with a missing `device_seq` as an empty string (E11).
- **SQL macros:** `{{ identifier_key(x) }}`, `{{ match_kind(m, g) }}`, `{{ split(k) }}`, `{{ suffix_lens() }}` and so on,
  expanded by `bq_utils.expand_macros`. A test fails if a regex literal appears in `sql/labels/`.

### 3.2 Label tables (BigQuery, `vigil_md_model`)

| Table | Rows or note |
|---|---|
| `device_split` | One row per GUDID base key: stemmed `device_key` and `split`. Suffix siblings (1 to 3 characters, at most two levels) share a group. |
| `alias_table` | Train devices only, at least 3 DIs per pair, 1,756 pairs. |
| `positives`, `negatives_suffix`, `negatives_same_labeler`, `negatives_easy` | Intermediate tables of steps 02 to 04. |
| `label_set_v1` | 627,308 rows. 261,691 MATCH, 365,617 NON_MATCH. |
| `label_stats_counts`, `label_stats_years`, `label_stats_fill` | Per split, label and match kind. Also the year weights and fill rates. |

`label_set_v1` by source: `udi_link` 261,691 (MATCH); `same_labeler_hard` 303,371, `random_labeler` 39,049 and
`suffix_sibling` 23,197 (NON_MATCH). Snapshot 2026-11-07, `easy_anchor_pct` 25. Parameters and hashes are in the
manifest.

### 3.3 Build script behavior
- Every part is dry-run before it runs and every job carries `maximum_bytes_billed` (`--max-gb`, default 25).
  Exit codes: 0 ok, 2 broken SQL or config, 3 over budget, 4 fixture differs.
- It writes only to `vigil_md_model` (a write guard checks each statement).
- Steps 02 and later refuse to run unless `alias_table` in BigQuery hashes to the value in the manifest. This keeps the
  alias table train-only.
- Chained parts cannot be priced until the earlier tables exist, so they are estimated just before they run.

### 3.4 Hand-label sample (batch 1)
- 150 MAUDE rows from the test split with no UDI, in 19 strata. About 35% are hard cases, 7 to 8 rows per reason (E3,
  E3b, E4, E5, E6, E7, E9). Each row has a rank-0 line ("none of these") and up to 3 ranked candidates.
- 46 rows have no candidate. Candidates come from both splits (118 train, 84 test). `cand_split` lets a report keep
  only test-split candidates.
- `meta_sample_weight` is rows in the stratum divided by rows drawn. Every rate must use it.

## 4. Verification so far

| Check | Result |
|---|---|
| Offline tests | 352 pass (no GCP calls) |
| Python and BigQuery parity (`--fixture`) | 45 cases, 0 differences |
| `device_split.n_bad_split` | 0 |
| `label_set_v1` split check | Build stops on any row without a split. It passed. |

The fixture found one real bug: RE2's `\s` has no vertical tab, and Python's ASCII `\s` does. Python now strips the same
five characters as BigQuery. BigQuery behavior and the built tables did not change. Other probes (non-breaking spaces at
the ends, ß) agree. Not yet recorded here: the other values of `device_split_summary` (device count, test share).

## 5. Known issues and open decisions

1. **`conflict` always means NON_MATCH.** A model that sees `feat_match_kind` can learn that. Report with and without it.
2. **Rule-label scores are an upper bound** for no-UDI rows. Positives all have a UDI.
3. **Negatives are assumed.** Some same-labeler, different-model pairs may be the same device under another key.
4. **Easy negatives are 6% of rows.** The plan says both "~10%" and "0.2 easy". We read it as 10% of negatives. Confirm.
5. **Open for sign-off (marked in code):** `NON_DISTINCTIVE_MAX_DIS` = 20 and `ALIAS_MIN_DIS` = 3.
6. **Hand-labeled devices are not forced into the test split.** The handoff said they would be. That conflicts with
   check C6, which recomputes the split from the key. Decide which one wins.
7. **`feat_conflicting_identifier` needs `sibling_keys`**, which `label_set_v1` does not carry. Without it the feature is
   always False. Build the keys from GUDID.
8. **Limits of two rules:** token match compares one whitespace-delimited word, and the kit-word regex is copied from
   query 13.
9. **Residual split leak:** GUDID's own aliases can put one device in both splits (see `alias_split_diagnostic`).
10. **No label validator in the repo yet** (item N4).

## 6. Ongoing work and next steps

**Hitika**
- Label all 352 lines of `labels/hand_label_sample_v1.csv` (guide section 3), then stop and review the guide before
  batch 2 is drawn.
- Compare hand labels with rule labels (weighted, test-split candidates only).

**Nandita**
- Build and run the validator on `label_set_v1` (C1 `pair_id`, C2 and C6 split, C3 labels, C5 no leak, C7 dedup, C4 stats).
- Label a copy of the 52 hard-case rows without seeing Hitika's labels
  (`labels/hand_label_sample_v1.labeler2.csv`), then agree labels and report Cohen's kappa.
- Settle open items 5 and 6.

**Satwik** — PR #33 is merged into `same-device-profiling`; its code now lives in `scripts/`.
- The separate feature code is gone. Everything calls `same_device_features.pair_features` and
  `FEATURE_COLUMNS`: one definition of the rules.
- The feature table is built by `scripts/s04_build_feature_table.py`. It reads the parquet that
  `s03_export_label_set.py` downloads, takes alias pairs from `labels/alias_table_v1.csv`, and builds
  `sibling_keys` from GUDID with one ~0.3 GB query, cached in `data/sibling_keys.csv`.
- `scripts/s06_train_models.py` trains on `meta_split = 'train'`, groups validation by
  `meta_device_key`, and now also scores the held-out `meta_split = 'test'` at the validation cutoff.
- Close `model-dev/eval-script` (#26), `model-dev/features` (#30) and `model-dev/xgboost` (#31).
- Train with and without `feat_match_kind`. Log `constants_sha256`, `alias_table_sha256` and `snapshot_date` from the
  manifest in MLflow. Evaluate on the hand labels with `meta_sample_weight` and `cand_split = test`.

| `pair_features` input | `label_set_v1` column |
|---|---|
| MAUDE labeler, name, model, catalog, product code | `key_m_mfr`, `key_m_name`, `key_m_model_raw`, `key_m_catalog_raw`, `key_m_pc` |
| GUDID labeler, name, model, catalog, product codes | `key_g_mfr`, `key_g_name`, `key_g_model_raw`, `key_g_catalog_raw`, `key_g_pcs` |
| `n_dis_for_model` | `meta_n_dis_for_model` |
| target and groups | `label`, `meta_device_key` |

## 7. Rules for everyone

- Write a rule in `same_device_features.py` only. If you need a new macro or constant, ask Hitika.
- A change to a rule constant makes the fixture stale: regenerate it (`golden_fixture.py --regen`), read the diff, run
  `s01_build_label_set.py --fixture`, rebuild the tables and bump the label-set version.
- Do not edit `bq_utils.py` unless you are Nandita. Do not relax the write guard.
- Do not run anything for Claude. Claude runs `--print-only` and pytest only. A person runs BigQuery.
- Do not report a metric without the items listed in `LABELS.md` section 6.

## 8. Run commands (WSL, from `Model-Development/`)

```bash
PY=~/.venvs/vigil-eval/bin/python
$PY scripts/s01_build_label_set.py --project vigil-md-1792696816 --fixture
$PY scripts/s01_build_label_set.py --project vigil-md-1792696816 --only 02 03 04 05 06 --estimate-only
$PY scripts/s01_build_label_set.py --project vigil-md-1792696816 --only 02 03 04 05 06 --snapshot-date <date>
~/.venvs/vigil-eval/bin/python -m pytest -q
```
