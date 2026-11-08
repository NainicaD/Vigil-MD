# Which table is which

Every BigQuery table and local file this folder reads or writes, what it is for, and which stage
makes it. If a table is not listed here, nothing in `Model-Development/` uses it.

Two datasets matter. `vigil_md_prod` is the pipeline's output and is **read-only** here.
`vigil_md_model` is the only dataset any script may write to, and `bq_utils.assert_model_write_only`
enforces that.

## The two tables you actually query

| Table | Rows | What it is |
|---|---|---|
| `vigil_md_model.label_set_v1` | 627,308 | **The training labels.** One row per pair, `MATCH` or `NON_MATCH`, with `key_*` raw inputs and `meta_split`. No features: those are computed in Python, so the UDI has no path into one |
| `labels/hand_label_sample_v1.csv` | 352 lines, 150 MAUDE rows | **The honest test.** People labeled it with `HAND_LABEL_GUIDE.md`. Not in BigQuery. Rows have no UDI, which is the population we actually care about |

Everything else below is scaffolding for those two.

## `vigil_md_prod` (read-only, made by Data-Pipeline)

| Table | Used for |
|---|---|
| `maude` | The adverse-event reports. The left side of every pair |
| `gudid` | The device catalog. The right side of every pair, and the source of sibling keys |
| `recall`, `enforcement`, `maude_problems`, `maude_text` | Not used by this folder |

`vigil_md_dev` is a small copy of `vigil_md_prod` with the same table names, covering 5 product
codes (LZG, BZD, OZD, FTR, PKU) and MAUDE from 2023 on. Use it to test a query cheaply:
`s04_build_feature_table.py --gudid-dataset vigil_md_dev --limit 5000`.

## `vigil_md_model` (written by `pipeline/s01_build_label_set.py`)

Run in this order; each depends on the one above.

| Table | Rows | What it is | Query it directly? |
|---|---|---|---|
| `device_split` | one per GUDID base key | `train` or `test` per device group. Suffix siblings share a group, so `GP200` and `GP200S` can never land on opposite sides | Only to debug a split |
| `alias_table` | 1,756 pairs | Company names that mean the same company. Built from **train devices only**. `s01` refuses to run if it does not match the hash in the manifest | No |
| `gudid_keyed` | — | GUDID with the identifier keys added | **No: intermediate** |
| `linked_all` | — | MAUDE rows joined to GUDID on primary DI | **No: intermediate** |
| `review_conflicts` | — | Linked rows whose identifiers contradict each other (E1), kept for human review | Only for review |
| `positives` | 261,691 | UDI-confirmed matches, capped at 200 rows per device | **No: use `label_set_v1`** |
| `negatives_suffix` | 23,197 | Proven E3a non-matches: same labeler, model differs by 1-3 trailing characters | **No: use `label_set_v1`** |
| `negatives_same_labeler` | 303,371 | Same labeler and product code, different model | **No: use `label_set_v1`** |
| `negatives_easy` | 39,049 | A different labeler, drawn at random | **No: use `label_set_v1`** |
| `label_set_v1` | 627,308 | The four above, deduped, with `pair_id` | **Yes** |
| `label_stats_counts`, `label_stats_years`, `label_stats_fill` | — | Counts for reports | For reports |

The four `label_source` values in `label_set_v1` name which source each row came from, so you
never need the intermediate tables to slice by it.

## Local files

| File | Made by | Committed? |
|---|---|---|
| `labels/label_set_v1.manifest.json` | `s01` | Yes. Hashes and parameters; quote `constants_sha256` and `snapshot_date` with any metric |
| `labels/alias_table_v1.csv` | `s01 --export-alias` | Yes. Must hash to the manifest value |
| `labels/hand_label_sample_v1.csv` | `s02`, then labeled by hand | Yes |
| `labels/hand_label_sample_v1.manifest.json` | `s02` | Yes. Stratum sizes and the sample weights |
| `data/label_set_v1.parquet` | `s03` | No, git-ignored |
| `data/sibling_keys.csv` | `s04` | No. A cache; delete it or pass `--refresh` to re-query |
| `data/mfr_rank.csv` | `s04` | No. A cache: the top 100 MAUDE manufacturers by report count, which give `meta_mfr_tier` |
| `data/label_set_v1_features.parquet` | `s04` | No. **The training table the model reads** |
| `data/hand_features.csv` | `s05` | No |
| `data/scores/*.csv`, `data/cutoffs.json` | `s06`, `s07` | No |
| `mlflow.db` | `s06`, `s08`, `s09` | No |

## Cost

The free tier is 1 TiB of query scan per month. These are the only billed reads:

| Stage | Scans |
|---|---|
| `s01_build_label_set.py` | ~25 GB for a full rebuild. Rarely needed |
| `s02_build_hand_label_sheet.py` | ~7 GB per batch |
| `s03_export_label_set.py` | **0.** `list_rows` is the row-listing API, not a query. A `SELECT *` would bill for the whole table |
| `s04_build_feature_table.py` | Two queries, once: sibling keys 0.221 GB, manufacturer ranking 0.441 GB (both measured by dry run). Cached in `data/` afterwards, so later rebuilds are free |
| `s05`–`s09` | **0.** Local files only |

Every script that queries takes `--print-only` (shows the SQL, no GCP call), `--estimate-only`
(free dry run) and `--max-gb` (refuses to run above it). Use them before any real run.
