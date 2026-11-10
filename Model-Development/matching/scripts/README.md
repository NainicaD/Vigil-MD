# Shared eval script

Every model is scored with `evaluate.py` and logged to MLflow the same way.

## Scores file (what each model writes)

| Column | Meaning |
|---|---|
| pair_id | Matches the pair table |
| score | Model's match score, 0 to 1 |
| model_name | e.g. xgb_v1 |
| mlflow_run_id | The run that made these scores |
| timestamp | When the scores were written (UTC) |

Labels live in a separate file and are joined by `pair_id`. Models never see labels.

## MLflow

- Experiment name: `vigil-md-linkage`
- Tracking: local `mlruns/` for now; shared server later

### Tags (every run)

| Tag | Values |
|---|---|
| track | baseline, xgb, embed, ft |
| model_name | e.g. rules_v1, logreg_v1, xgb_v1 |
| threshold_version | e.g. t1 |
| normalization_version | e.g. n1 |

### Metrics (every run)

| Metric | Meaning |
|---|---|
| precision_at_0.5 | Precision at cutoff 0.5 |
| recall_at_0.5 | Recall at cutoff 0.5 |
| f1_at_0.5 | F1 at cutoff 0.5 |
| cutoff_p99 | Lowest cutoff with 99% precision |
| recall_at_p99 | Recall at that cutoff |
| precision_slice_<name>_<value> | Precision per slice at cutoff_p99 |
| recall_slice_<name>_<value> | Recall per slice at cutoff_p99 |

## Open decisions

- How INSUFFICIENT_EVIDENCE labels are treated
- Review rate definition (proposed: two cutoffs, middle goes to human review)