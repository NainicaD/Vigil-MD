# How to make the linkage model learn the real features

This document answers five questions about the same-device model:

1. What does the model learn now?
2. Can embeddings fix it?
3. Can NER on the MAUDE narratives help?
4. Can a different model or more hyperparameter tuning help?
5. What must change, and in which order?

Every number comes from `pipeline/diag_training_issues.py` (diag version `d2`). You can find them in `reports/training_diag/RESULTS.md` and `summary.json`, and in MLflow under `tags.track = "diag"`. Hand-sheet precision comes from `pipeline/s08_evaluate_hand.py`. Code references use `path:line` from `Model-Development/`.

## Glossary

| Term | Meaning |
|---|---|
| Match kind | How the MAUDE identifiers relate to the GUDID identifiers. `identifier_match` sets it (`vigil_model/same_device_features.py:171-197`). Example values: `same_model_key`, `contains`, `prefix_related`, `conflict`. |
| Shortcut | A feature that predicts the label in the training data but not on the real target rows. |
| IE | `INSUFFICIENT_EVIDENCE`, the third hand label. `s08` removes IE rows from precision and recall. |
| Agree region | Hand pairs whose match kind says that the identifiers agree: `same_model_key`, the two cross-field kinds, `catalog_eq_catalog` and `contains`. |

## 1. Summary

The problem is in the labels, not in the model family.

- The training labels come from a rule. In 95.9% of training rows, the match kind alone gives the label (D1).
- The rule "MATCH if the match kind is all-MATCH in train" gets precision 1.000 on the label-set test split. No trained model is better (D2).
- On the hand sheet, `xgb_all` accepts 44 of 83 NON_MATCH pairs. Its precision is 0.617 (`s08`).
- The model also accepts 45 of 48 IE pairs (D4). `s08` cannot show this, because it removes IE rows (`pipeline/s08_evaluate_hand.py:85-86`).
- The training split and the test split share the same bias, so validation does not detect it. Validation AP minus test AP is at most 0.00047 (D9). This is shortcut learning, not classic overfitting. More regularization does not fix it.

## 2. What the model learns now (D10)

D10 refits each model from the params that MLflow logged for it. Then D10 compares the refit scores with the saved hand scores. All four models reproduce within 1e-6, so the tables describe the trained models. The method is XGBoost SHAP (`pred_contribs`), the same method as `pipeline/s10_explain_model.py:68-73`.

`xgb_all`, mean |SHAP| on the hand rows, and the mean push on its 44 false positives (positive = toward MATCH):

| Feature | Hand mean abs | Push on false positives |
|---|---|---|
| `feat_match_kind` | 5.642 | +4.482 |
| `feat_conflicting_identifier` | 3.751 | +2.758 |
| `feat_identifier_exact` | 1.242 | +0.069 |
| `feat_maude_model_missing` | 0.407 | +0.629 |
| `feat_labeler_equal` | 0.373 | -0.034 |
| `feat_labeler_alias_equal` | 0.253 | -0.196 |
| `feat_n_dis_for_model` | 0.174 | +0.144 |
| `feat_distinctive` | 0.000 | 0.000 |

What the numbers show:

1. The match kind and the identifier flags carry most of the score. The labeler features carry little weight. `feat_distinctive` carries none.
2. The hand-label guide decides most hard cases by labeler and by distinctiveness (`docs/HAND_LABEL_GUIDE.md` §3, steps 4 and 5). The model does not use these features.
3. `feat_conflicting_identifier = False` pushes the false positives toward MATCH. In training, only the `suffix_sibling` negatives have it set. So "no sibling conflict" works as evidence for MATCH.
4. `feat_maude_model_missing` pushes toward MATCH in all four models (+0.629 to +2.374). The cause is in the labels. All 11,631 training rows of kind `maude_model_missing_or_placeholder` are MATCH (`labels/label_set_v1.manifest.json`, `row_counts`).
5. The ablations move the shortcut to other columns. They do not remove it. In `xgb_no_kind_derived`, `feat_token_contains` and `feat_raw_identifier_equal` become the top features. A depth-4 tree on those 15 features predicts the label with accuracy 0.992 (D3).
6. Logistic regression puts its largest weight on `kind_conflict` (hand mean abs 6.153, push +6.362).

The root cause is in two SQL lines:

- `sql/labels/04_negatives_same_labeler.sql:102` keeps only negatives of kind `conflict`.
- `sql/labels/02_positives.sql:74` removes all positives of kind `conflict`.

So no training negative has agreeing identifiers. The model never sees a pair where the identifiers agree but the devices differ. Kind `contains` has 1,357 MATCH rows and 0 NON_MATCH rows in train.

## 3. Can embeddings fix it?

Not alone. Embeddings help as one extra feature in the agree region.

**What exists now.** `vigil_model/embedding_retrieval.py` is a retrieval prototype:

- It builds one text line per record from labeler, name, model, catalog and product code (`:77-89`).
- It scores Recall@k against the UDI link (`:8-9`).
- It finds candidates. It does not decide MATCH or NON_MATCH.
- No run results are in this folder: there is no `reports/embedding/` and no `data/embedding/`.

**What D11 measured.** D11 uses the prototype's own text template and its TF-IDF baseline (`:106-120`). It fits TF-IDF on 204,974 GUDID texts of the label set. Then it scores the cosine of each hand pair (`pipeline/diag_training_issues.py:378`).

| Subset | Rows | AUC MATCH vs NON_MATCH |
|---|---|---|
| All decided hand pairs | 154 | 0.725 |
| Agree region | 101 decided (113 with IE) | 0.998 |

In the agree region, the NON_MATCH pairs have a median cosine of 0.126. The MATCH pairs have a median of 0.778. Two error types cause these NON_MATCH pairs:

- **Substring hits.** The `contains` rule accepts a key of 3 or more characters inside a longer key (`vigil_model/same_device_features.py:195-196`). Example: MAUDE `1260` inside GUDID `h749195001260`. Eleven of the 30 agree-region NON_MATCH pairs have the same labeler.
- **Cross-labeler collisions.** Example: Dentsply catalog `24952` and ResMed model `24952`. Nineteen of the 30 have a different labeler.

The model scores these pairs at about 0.9999. Text similarity puts them far below the MATCH pairs.

**Where embeddings do not help.** Outside the agree region, the cosine carries no signal. NON_MATCH pairs of kind `conflict` have a median of 0.755. `prefix_related` NON_MATCH pairs have 0.743, and IE pairs 0.754. Text similarity cannot see that `GP200` and `GP200S` are different devices. The rules already handle these pairs.

**Limits of D11.**

- The sample is small: 30 NON_MATCH pairs in the agree region.
- The hand labels equal the s07 rule tree on 154 of 154 decided pairs (D8). So the AUC shows agreement with the rules, not ground truth.
- Only TF-IDF ran. D11 did not test a sentence-transformers model.

**Why a learned embedding is not a fix.** You can train a siamese model or a cross-encoder on `label_set_v1`. But that model sees the same labels. Those labels contain no agree-region negatives. So the model can learn the same shortcut from the text.

**Recommendation.**

1. Keep the embedding for candidate retrieval.
2. Add the TF-IDF cosine as one feature in `pair_features`.
3. Fit the vectorizer on train-split GUDID texts only, so the test split stays clean.

## 4. Can NER on the MAUDE narratives help?

Yes, for the rows with no usable identifier. The source exists but nothing uses it yet.

- `docs/TABLES.md:25` lists `vigil_md_prod.maude_text` as "not used by this folder".
- `Data-Pipeline/scripts/acquisition/fetch_sources.py:6` says the openFDA MAUDE source "includes narratives".
- `docs/HAND_LABEL_GUIDE.md:141` says the narrative "often names the exact model or component".

**Rows it can help:**

1. Placeholder rows. The model accepts 5 of 5 placeholder-kind pairs (D4).
2. Rows with no usable model. Precision on `strat_model_usable = FALSE` is 0.513 (`s08`).
3. IE rows. Many of them are IE because the structured fields are empty or are placeholders.

**Start with the cheap step.** Before you train an NER model, test a token match of the candidate's model and catalog numbers against the narrative. `contains_as_token` already does this (`vigil_model/same_device_features.py:158-164`). Add the result as one feature, for example `feat_narrative_names_candidate`. Train an NER model only if the token match finds too few identifiers.

**NER, if you need it.** Extract the model number, catalog number, lot number, component and brand. Use each extracted value as a second identifier source for `identifier_match`. Label a few hundred narratives first, to measure the extraction.

**Open check before any query.** The warehouse schemas in `Data-Pipeline/scripts/warehouse/schemas.py` do not show the `maude_text` columns. Do these steps in order:

1. Check the columns with `--print-only`.
2. Check the scan size with `--estimate-only`.
3. Run the real query only after both checks.

Narrative text is long, so the scan size can be large.

## 5. Can a different model help?

Not until the labels change. Three facts show this:

1. The rule alone already has test precision 1.000 (D2). The models cannot be better on this test split.
2. A depth-4 tree reaches label accuracy 0.992 without the kind-derived features (D3). Any flexible model finds the same rule.
3. The false positives come from pairs that are absent from training (section 2). No model family can learn a pattern that the labels do not contain.

After you change the labels, two model options are useful:

- **Monotone constraints in XGBoost.** Make the score non-decreasing in `feat_labeler_equal`, `feat_labeler_alias_equal` and `feat_distinctive`. This stops the model from learning a wrong direction from a biased sample.
- **A small fine-tuned cross-encoder** on the pair text. Use it only after the hard negatives from section 7 exist. Compare it with XGBoost plus the cosine feature on the same hand split.

## 6. Can hyperparameter tuning help?

No. The validation set gives no signal to tune on.

- `s06` selects the trial with the best validation AP (`pipeline/s06_train_models.py:268-283`).
- The validation AP is 0.99996 for `xgb_all` and 0.99996 for `logreg_v1` (MLflow, `val_average_precision`). Every trial looks equally good.
- The validation cutoffs for 99% precision are 0.025 to 0.212 (`data/cutoffs.json`). Nearly every positive score is above the cutoff.

Tuning becomes useful when validation contains the hard cases. Two changes do this:

1. **A hard validation set.** Label a second hand batch for model selection only. Keep batch 1 as the test set. The cutoff then still does not come from the test labels (`pipeline/s08_evaluate_hand.py:8`).
2. **A grouped split by labeler.** `s06` groups validation by device (`pipeline/s06_train_models.py:150-151`). A split by labeler also tests cross-labeler collisions.

## 7. What to change, in order

Each change names the diag metric that must move. Run `pipeline/diag_training_issues.py` after each change and compare the `diag` runs in MLflow.

| # | Change | Where | Metric that must move |
|---|---|---|---|
| 1 | Add hard negatives with agreeing identifiers. Pair each UDI-linked anchor with GUDID records outside its model group. Use three kinds: the same model key at a different labeler with no alias, `contains` substring hits, and non-distinctive keys. The UDI proves that these records are a different device. | `sql/labels/04_negatives_same_labeler.sql:102`, a new negatives query | `d1_kind_purity_contains` falls below 1. `d4_accept_rate_label_NON_MATCH` falls. |
| 2 | Stop training "missing model = MATCH". Add placeholder anchors paired with other models of the same labeler as NON_MATCH, or remove placeholder rows from the positives. | `sql/labels/02_positives.sql` | `d1_kind_purity_maude_model_missing_or_placeholder` falls below 1. `d10_fp_push_feat_maude_model_missing` falls. |
| 3 | Put an IE gate before the model. Use the s07 step 1 rule: no usable MAUDE model and no usable catalog means IE. | `pipeline/s07_rules_baseline.py:36-38`, scoring | `d4_accept_rate_label_INSUFFICIENT_EVIDENCE` falls. |
| 4 | Report the IE accept rate in `s08`, next to precision and recall. | `pipeline/s08_evaluate_hand.py:85-86` | The number becomes visible in every hand run. |
| 5 | Make hand labels without the helper columns. Then the labels do not copy the rules. | `docs/HAND_LABEL_GUIDE.md` §1 and §8 | `d8_hand_rules_agree_rate` falls below 1.0 |
| 6 | Add the TF-IDF cosine as a feature (section 3). | `vigil_model/same_device_features.py:230`, `pipeline/s04`, `pipeline/s05` | `d10_shap_hand_feat_text_cosine` becomes greater than 0. |
| 7 | Add the narrative token match, then NER if needed (section 4). | new feature, `maude_text` | Precision on `strat_model_usable = FALSE` (`s08`) rises. |
| 8 | Reweight the training rows by match kind, so the pure kinds do not dominate. This helps only after change 1 adds mixed rows. | `pipeline/s06_train_models.py:258`, `:278`, `sample_weight` | `d3_label_acc_no_kind_derived` falls toward the hand-sheet accuracy. |
| 9 | Tune on a hard validation set (section 6). | `pipeline/s06_train_models.py:150-151` | Validation AP falls below 0.999 and starts to rank the trials. |

Do changes 1 to 4 first. They fix the labels and the measurement. Changes 6 and 7 add new evidence. Changes 8 and 9 have an effect only after change 1.

## 8. Limits of this analysis

- The hand sheet has 202 candidate pairs. Hard-case groups have 3 to 20 pairs each.
- The hand labels equal the s07 rule tree on 154 of 154 decided pairs (D8). The hand metrics therefore measure agreement with the rules.
- D10 uses refit models, not the artifacts on GCS. The refits reproduce the saved hand scores within 1e-6.
- D11 ran TF-IDF only. A sentence-transformers model may give different numbers.
- The hard-negative and placeholder changes in section 7 are proposals. This analysis did not test them.
