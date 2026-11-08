# Labels for same-device matching

What the label tables are, how they were made, and what a number built on them does and does not mean.
Read this before you report any metric. Labeling rules: [`SAME_DEVICE_DEFINITION.md`](SAME_DEVICE_DEFINITION.md).
How to label the hand sheet: [`HAND_LABEL_GUIDE.md`](HAND_LABEL_GUIDE.md).

## 1. Two label sets

| | Rule labels (`label_set_v1`) | Hand labels (`hand_label_sample_v1.csv`) |
|---|---|---|
| Where | BigQuery `vigil_md_model.label_set_v1` | `labels/hand_label_sample_v1.csv` |
| Made by | `pipeline/s01_build_label_set.py` | people, with the guide |
| Values | `MATCH`, `NON_MATCH` | `MATCH`, `NON_MATCH`, `INSUFFICIENT_EVIDENCE` |
| Rows | 627,308 | 352 lines for 150 MAUDE rows (batch 1) |
| Use | training and a first validation | the honest test: no-UDI MAUDE rows |
| Ground truth? | No. A rule produced it. | Closest we have. Still a human judgment. |

The rule labels are for training. The hand labels are for measuring. Do not report a model score
from the rule labels as if it described real MAUDE rows without a UDI.

## 2. How `label_set_v1` is built

Snapshot 2026-11-07. Parameters and hashes are in `labels/label_set_v1.manifest.json`.

| `label_source` | Label | Rows | What it is |
|---|---|---|---|
| `udi_link` | MATCH | 261,691 | A MAUDE row whose UDI equals the GUDID primary DI. |
| `same_labeler_hard` | NON_MATCH | 303,371 | Same labeler (or alias) and product code, different model. |
| `random_labeler` | NON_MATCH | 39,049 | A different labeler, drawn at random (easy negatives). |
| `suffix_sibling` | NON_MATCH | 23,197 | Same labeler, model differs by 1 to 3 trailing characters (GP200 vs GP200S). |

- **Positives** drop two kinds of row. First, rows where the identifiers contradict each other
  (match kind `conflict`). Second, rows that agree only on a model key shared by more than 20 DIs and
  have no catalog agreement. They keep at most 200 rows per device.
- **Negatives** all sit in the same split as their anchor positive. Suffix siblings are only built from
  UDI-confirmed positives, so each one is a proven NON_MATCH (E3a). A bare prefix with no anchor is not
  a negative (E3b).
- **Dedup:** one row per (MAUDE labeler, model key, catalog key, product code, DI). Positives win ties.
- **Columns:** `key_*` join and audit fields (the raw inputs), `label`, `label_source`, `meta_*` (split,
  device key, year, duplicate count). There are **no `feat_*` columns** in the table. Features are
  computed in Python by `same_device_features.pair_features`, so the DI cannot reach a feature.

## 3. The split

- The unit is the **device group**, not the DI and not the labeler. `meta_split` is
  `SHA-256("v1|" + device_key)`, with 20% of device groups in `test`.
- Suffix siblings are merged into one group (at most two levels), so GP200 and GP200S land on the
  same side. Otherwise a test negative would have been seen in training.
- The alias table (`labels/alias_table_v1.csv`) is built from **train devices only**, with at least 3
  DIs per pair. `build_labels.py` refuses to build labels if the table in BigQuery does not match the
  hash in the manifest.
- Residual leak, accepted: GUDID's own aliases (for example zimmer dental and zimvie) can put one
  physical device in both splits under two names. `alias_split_diagnostic` sizes it.

## 4. Hand labels

- Batch 1 is 150 MAUDE rows from the test split with no UDI. About 35% are hard cases (E3b, E4, E5, E6,
  E7, E9), over-sampled on purpose. Each row has a rank-0 line ("none of these") and up to 3
  candidates.
- Every rate must be weighted by `meta_sample_weight` (rows in the stratum / rows drawn). An unweighted
  rate describes the sample, not the no-UDI pool.
- Candidates come from both splits. Filter to `cand_split = test` for strict numbers.
- `INSUFFICIENT_EVIDENCE` rows are left out of precision and recall and reported as a count.
- A second labeler labels the hard rows, and Cohen's kappa is reported (guide section 8).

## 5. Limits you must state with any metric

1. **`conflict` always means NON_MATCH.** No positive has match kind `conflict`, and every same-labeler
   and random negative does. A model that sees `feat_match_kind` can learn this shortcut. Report results
   with and without that feature.
2. **Scores on this set are an optimistic upper bound.** Positives are rows that have a UDI. The target
   is rows without one, which are formatted differently and are harder.
3. **Negatives are assumed, not checked.** A same-labeler, different-model pair is called NON_MATCH. A
   few may be the same device under another key (an alias, a catalog number in the model field).
4. **Easy negatives are about 6% of rows** (10.7% of negatives). Hard negatives are 89% of negatives.
   Class balance is 42% MATCH, 58% NON_MATCH, and it is not the balance of real MAUDE.
5. **Token match** compares one whitespace-delimited word only. "IP 200" split across words is missed.
6. **Kit and system words** use one regex copied from query 13.
7. `feat_conflicting_identifier` needs the labeler's GUDID keys (`sibling_keys`), which the table does not
   carry. `pipeline/s04_build_feature_table.py` fetches them from GUDID when it builds the feature table,
   so the feature is live there. Anything that calls `pair_features` on `label_set_v1` **without** passing
   `sibling_keys` gets False on every row, and the 23,197 `suffix_sibling` negatives then teach nothing.

## 6. When you report a number, say

- which labels (rule or hand), which split, and whether you used `cand_split = test`;
- whether rates are weighted by `meta_sample_weight`;
- whether `feat_match_kind` was in the model;
- the manifest values: `constants_sha256`, `alias_table_sha256`, `snapshot_date`.

## 7. Rebuild and checks

```bash
PY=~/.venvs/vigil-eval/bin/python
$PY pipeline/s01_build_label_set.py --project vigil-md-1792696816 --fixture          # Python and BigQuery agree
$PY pipeline/s01_build_label_set.py --project vigil-md-1792696816 --estimate-only
$PY pipeline/s01_build_label_set.py --project vigil-md-1792696816 --only 02 03 04 05 06 --snapshot-date <date>
```

- Change a rule constant and the fixture goes stale: regenerate it, read the diff, run `--fixture`, then
  rebuild the tables and bump the label-set version.
- Offline tests: `~/.venvs/vigil-eval/bin/python -m pytest -q`.
