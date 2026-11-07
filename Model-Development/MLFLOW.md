# MLflow conventions for Model-Development

Both workstreams (`chatbot/` and `matching/`) track experiments and register models the same
way, so runs can be compared and the report can cite them. MLflow 3.18.0, free, runs on a
laptop. Reference implementation: [`chatbot/question_classifier/tracking.py`](chatbot/question_classifier/tracking.py)
(copy it into your workstream; change `WORKSTREAM`, `EXPERIMENT` and `REGISTERED_MODEL`).

## Setup

```bash
conda activate vigil            # or your workstream's environment
pip install mlflow==3.18.0
```

## Where runs are stored

| What | Where |
|---|---|
| Runs, parameters, metrics, registry | `Model-Development/mlflow/mlflow.db` (SQLite) |
| Artifacts (model files, reports) | `Model-Development/mlflow/artifacts/<experiment>/` |

- One store per machine, **not in git** (`.gitignore`). Each teammate has their own until we
  choose a shared server.
- `MLFLOW_TRACKING_URI` (and `VIGIL_MLFLOW_ARTIFACTS` for artifacts) override the defaults, so
  moving to a shared server later needs no code change.
- Create experiments with their artifact folder under the store (see `tracking.setup()`).
  Otherwise MLflow writes artifacts to `./mlruns` in whatever folder you run from.
- Tests must log to a temporary store (see `chatbot/tests/conftest.py`), never to yours.

## Look at runs

```bash
cd ~/Vigil-MD
mlflow ui --backend-store-uri sqlite:///Model-Development/mlflow/mlflow.db
# open http://localhost:5000
```

## Naming

| Thing | Convention | Examples |
|---|---|---|
| Experiment | `<workstream>/<task>` | `chatbot/question-classifier`, `matching/maude-gudid-pairwise` |
| Parent run (one training session) | `<model>-<what varies>` | `tfidf_lr-grid` |
| Nested run (one setting tried) | `<model>_<setting>` | `tfidf_lr_C2.0` |
| Registered model | `<workstream>-<task>` | `chatbot-question-classifier`, `matching-maude-gudid` |

## What every run records

**Required tags** (`tracking.REQUIRED_TAGS`):

| Tag | Value |
|---|---|
| `workstream` | `chatbot` or `matching` |
| `git_commit` | `git rev-parse HEAD` |
| `git_dirty` | `true` if there were uncommitted changes: such a run can't be reproduced exactly |
| `data_version` | 12-character SHA-256 of the training data files; for BigQuery data, the tables and the date or snapshot used |
| `author` | who ran it (`VIGIL_AUTHOR`, else the login name) |

Add task-specific tags too (the chatbot adds `rules_fingerprint`, the guardrail rules version).

**Parameters:** everything that changes the result: model type, hyperparameters, seeds, feature
settings, data sizes, thresholds or the rule used to pick them.

**Metrics:** prefixed by the data split, so a number always says where it was measured:

| Prefix | Data | Used for |
|---|---|---|
| `val_` | validation | choosing settings and thresholds |
| `heldout_` | held-out split, never used for tuning | the honest estimate |
| `test_` | hand-written or labelled test set | the final number for the report |

Each workstream adds its key numbers under these prefixes. Chatbot: `advice_recall`,
`false_refusal_rate`, `macro_f1`. Matching (suggested; target is 99% precision):
`precision`, `recall`, `f1` at the chosen threshold, `threshold`, number of candidate pairs.

**Artifacts:** the model (`mlflow.<flavor>.log_model`, which also registers it), the files
the code loads (e.g. `model.joblib`, `meta.json`), and evaluation reports as JSON.

## Model registry

- Each training run registers a new version of its registered model.
- Aliases mark the role: `champion` (the model in use), `challenger` (a candidate being
  compared). Move an alias only after comparing `heldout_` / `test_` metrics.
- Write the run id and model version into the model's own metadata (the chatbot's `meta.json`
  has `mlflow_run_id` and `model_version`), so any saved model points back to its run.

## Evaluation goes on the training run

Log held-out and test metrics to the **same run** that trained the model (reopen it with
`mlflow.start_run(run_id=...)`), so one run shows validation and held-out numbers side by side.

## Never log

Credentials, `.env` values, service-account keys, or raw data beyond small samples.

## Later: a shared store

When the team needs one view of all runs, point `MLFLOW_TRACKING_URI` at a shared server
(options and costs to be decided; a GCP VM costs money, so check the budget first).
