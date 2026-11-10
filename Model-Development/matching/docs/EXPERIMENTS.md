# Experiments

One row per experiment. Each experiment changes one thing (labels or one feature) and is compared
against the baseline on the same scorecard. The why and the plan for each id is in
`MODEL_IMPROVEMENT.md` §7 and in `reports/training_diag/RESULTS.md`.

## How to run one

```bash
export VIGIL_EXP=E1-agree-neg           # or pass --exp to s06, s08 and the diag script
python scripts/s06_train_models.py      # writes data/runs/$VIGIL_EXP/{scores/,cutoffs.json}
python - <<'EOF'                        # s08 for every model of this experiment
import json, os, subprocess, sys
run = f"data/runs/{os.environ['VIGIL_EXP']}"
for name, cutoff in json.load(open(f"{run}/cutoffs.json")).items():
    if cutoff is None:
        continue
    track = "baseline" if name.startswith("logreg") else "xgb"
    subprocess.run([sys.executable, "eval/s08_evaluate_hand.py", "--scores", f"{run}/scores/{name}_hand_scores.csv",
                    "--cutoff", str(cutoff), "--track", track, *sys.argv[1:]], check=True)
EOF
python scripts/diag_training_issues.py  # reads data/runs/$VIGIL_EXP, writes reports/training_diag/$VIGIL_EXP
```

Every experiment's scores stay in `data/runs/<exp>/`, so runs never overwrite each other. Without
`VIGIL_EXP`, s06 writes `data/scores/` and `data/cutoffs.json` as before (CI reads those).
`data/runs/P0-rerun/` holds the baseline.

Every run in `mlflow.db` (s06, s08, s09, s10, diag, embedding retrieval) carries the tags `exp`,
`git_commit`, `label_set_manifest_sha256` and `caps_*` (the label set's caps and ratios, for example
`caps_easy_anchor_pct`). Compare:

```python
import mlflow
mlflow.search_runs(experiment_names=["vigil-md-linkage"], filter_string="tags.exp = 'E1-hard-neg'")
```

Never overwrite a label set. A new label set gets a new version (`label_set_v2`, ...), so the
baseline stays reproducible.

## Scorecard

| Metric | Source | Good direction |
|---|---|---|
| hand precision, all and strict | s08 `all_precision`, `strict_precision` | up |
| IE accept rate | s08 `ie_accept_rate` | down |
| NON_MATCH accept rate | diag `d4_accept_rate_label_NON_MATCH` | down |
| kind purity of `contains` / placeholder | diag `d1_kind_purity_*` | below 1 |
| label accuracy without kind features | diag `d3_label_acc_no_kind_derived` | down, toward hand accuracy |
| precision where the model is not usable | s08 `slice_strat_model_usable_FALSE_precision` | up |

Keep a change only if the scorecard gets better on hand batch 2 (selection) and does not get worse on
hand batch 1 (test). Until batch 2 exists, batch 1 is the only hand set: say so in the result.

## Registry

| Id | Change | Label set | Must move | Runs (`tags.exp`) | Result |
|---|---|---|---|---|---|
| P0 | Baseline: `xgb_all` on label_set_v1, `easy_anchor_pct=25`, git `7ed8d34` | v1 | n/a | runs before the `exp` tag (untagged) | precision 0.617, IE accepted 45/48 (0.938), NON_MATCH accepted 44/83 |
| P0-rerun | Baseline again with the split validation (trial half / cutoff half). Re-logged on 2026-11-09 after `mlflow.db` was lost | v1 | n/a | `P0-rerun` | `xgb_all` cutoff 0.0264: precision 71/113 = 0.628, IE accepted 45/48, NON_MATCH accepted 42/83. `xgb_no_kind_derived`: 0.676, IE 25/48, NON_MATCH 34/83. This is the baseline from here on. |
| E4 | IE gate before the model: `insufficient_evidence()` = no usable MAUDE identifier (E6) or kind `prefix_related` (E3b). `s08 --ie-gate` | v1 | IE accept rate | `E4-ie-gate` (s08 `--ie-gate`, logged) | **Keep.** `xgb_all`: IE 45/48 → 15/48, precision 0.628 → 0.689, hand recall stays 1.000. Cost: 2.66% of test-split MATCH rows are `prefix_related` and would go to IE. The s07 step-1 rule alone catches 0 of 48 IE rows on this sheet; 30 of 48 IE rows are `prefix_related`. Circular: the hand labels follow the same guide (D8). |
| E1 | Agree-region hard negatives `04b_negatives_agree.sql`: (a) exact key at another labeler with no alias and a different product code, (b) `contains` hit at the same labeler family and product code, (c) same non-distinctive model key with a conflicting catalog | `label_set_v2_e1` | `d1_kind_purity_contains` < 1, NON_MATCH accept rate | `E1-agree-neg` | **Keep.** Agreeing NON_MATCH rows added (train): `same_model_key` 19,633, `maude_catalog_eq_gudid_model` 12,556, `contains` 4,233, `maude_model_eq_gudid_catalog` 2,881, `catalog_eq_catalog` 1,412. D1 single-label share 0.9588 → 0.5286. Hand, `xgb_all`: precision 0.628 → 0.835, NON_MATCH accepted 42 → 14/83; `logreg_v1`: 0.602 → 0.933 (5 FP), but recall 0.986 (misses one same-company pair with differently spelled labeler names). IE accepted unchanged (44/48): E1 does not touch IE. Validation AP 0.99996 → 0.9989, so tuning has some signal now. **E1 + E4 gate** (s08 `--no-mlflow`, not logged): `logreg_v1` 0.972 (2 FP, lower bound 0.915), `xgb_no_match_kind` 0.910, `xgb_all` 0.866; IE 14/48. Remaining `xgb_all` FPs: 9 `contains` (score ≈ 0.99, few distinct feature vectors), 3 `prefix_related` (the gate removes them), 2 placeholder kind (target of E3). |
| E2 | TF-IDF neighbours that are not the linked device, as negatives | v2-e2 | NON_MATCH accept rate | | |
| E3 | Placeholder anchors paired with other models of the same labeler, as negatives | v2-e3 | `d1_kind_purity_maude_model_missing_or_placeholder` | | |
| E5 | Hand batch 2, labeled blind, stratified by the embedding slices | n/a | `d8_hand_rules_agree_rate` | | |
| E6 | Wrong-UDI-link audit of `misses_tfidf.csv` | n/a | wrong-link rate | | |
| F1 | `feat_text_cosine`: TF-IDF char 3-5-gram cosine of the two record texts, encoder fit on train-split GUDID texts only, saved as `<features>.tfidf.pkl` and reused by s05 | `label_set_v2_e1` | `d10` SHAP of the feature > 0; `contains` false positives fall | `F1F2-text-contains` (one run with F2) | Offline check before training: the 9 `contains` hand FPs have cosine 0.09-0.38; train `contains` MATCH median 0.728 vs NON_MATCH 0.514 |
| F2 | `feat_contains_extra_len`, `feat_contains_at_end` (None unless kind `contains`; both in `KIND_DERIVED`). Planned ratio/position replaced: on train, extra length separates (1-3 extra characters: 87% NON_MATCH) and the ratio does not | `label_set_v2_e1` | agree-region false positives | `F1F2-text-contains` | |
| F3 | `feat_labeler_sim` (labeler name n-gram cosine) | v2 | cross-labeler false positives | | |
| F4 | `feat_narrative_names_candidate` (needs `maude_text`) | v2 | `strat_model_usable = FALSE` precision | | |
| F5 | Monotone constraints on the labeler and distinctiveness features | v2 | NON_MATCH accept rate | | |

Note: from P0 on, s06 picks the XGBoost trial on one half of validation and the cutoff on the other
half (they used to share one fold). So new cutoffs can differ a little from the P0 cutoffs for
reasons other than the experiment. Run P0 once more with `VIGIL_EXP=P0-rerun` and compare against that.
