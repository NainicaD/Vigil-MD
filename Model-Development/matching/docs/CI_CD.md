# CI/CD for model training

Workflow: [`.github/workflows/model-training.yml`](../../.github/workflows/model-training.yml).
When code under `Model-Development/` is committed, GitHub Actions tests it, checks that the training
code runs, and (on the shared branches) retrains the models on the real BigQuery data.

## 1. What runs when

| Job | Runs on | Uses GCP | What it does | Time |
|---|---|---|---|---|
| `test` | every pull request and push that touches `Model-Development/` | no | `python -m pytest`: the mocked tests | ~2 min |
| `smoke-train` | same | no | `ci/make_ci_feature_table.py` makes a synthetic feature table, `s05` builds the hand-sheet features from the committed sheet, `s06` trains all four models on the synthetic table (1 trial, no MLflow), `ci/check_training_outputs.py` checks the outputs, `s08` scores the hand sheet, `ci/gate.py --report-only` proves the gate runs (never fails here) | ~3 min |
| `train` | push to `main`, `model-dev-integration` or `same-device-profiling` (a merged PR), or **Actions → model-training → Run workflow** | yes | `s03 → s04 → s05 → s06 → s08` on the real label set, checks the outputs, runs the **model gate**, uploads the results; on a push, if the gate passed, **publishes** the model to Artifact Registry (`s11`) | 20-60 min |

- `train` needs `test` and `smoke-train` to pass first.
- A newer push to the same branch cancels the older run (`concurrency`).
- `train` is skipped, not failed, until the GCP setup in section 3 is done.
- The smoke run's numbers mean nothing: the data is synthetic. It only proves the code runs end to end.

## 2. What a training run produces

- **Run page summary:** each model's 99%-precision cutoff, its validation and test metrics, and its
  hand-label metrics from `s08`.
- **Artifact** `training-<run number>-<commit>` (kept 90 days): `mlflow.db` and `mlruns/` (the
  MLflow runs, with the XGBoost models), `data/cutoffs.json`, `data/scores/` (hand scores, the s08
  `*_hand.json`, `gate.json`, `bias_report.md`, `bias_slices.png`), and the feature-table manifest.
  A version that CI publishes carries the annotation `mlflow=gh-run-<run id>`: this artifact is its
  MLflow record. To look at the runs, download the artifact, then from that folder:

  ```bash
  mlflow ui --backend-store-uri sqlite:///mlflow.db
  ```

- The job **fails** (exit 3) if training leaves incomplete or broken outputs: no `cutoffs.json`, a model
  without scores, scores that do not match the hand sheet, a score outside [0, 1], or a model with no
  MLflow run. That check does not look at metric values.
- **Model gate** (`ci/gate.py`, rules in `ci/gate.json`, reference numbers in `ci/champion_metrics.json`
  = release v1.1.0, IE gate `e6_e3b_c1`): `xgb_no_match_kind` must meet the floors (hand recall ≥ 0.99, strict hand precision
  ≥ 0.90, test precision ≥ 0.98), stay under the ceilings (IE and NON_MATCH accept rates, and the widest
  precision / recall gap between the `strat_model_usable` and `strat_mfr_size` slices), and be no worse
  than the champion by more than the tolerances. A ceiling the champion already breaks only warns if the
  new model is no worse (v1.0.0 broke the precision-gap ceiling with 0.158; v1.1.0's gap is 0).
  If a rule fails, the job fails and nothing is published: the champion stays. That is the rollback.
- **Publish** (`s11 --semver auto --lineage ci`, push only, after the gate passed): the next PATCH
  version of `matching-same-device` in `vigil-md-models`, annotated `mlflow=gh-run-<id>`,
  `git=git-<sha7>`. `ci/publish.py` refuses again if `gate.json` did not pass or the tree has
  uncommitted files, and skips a version that already exists.

## 3. One-time GCP setup (done 2026-11-13)

GitHub gets a short-lived token through **Workload Identity Federation**. No service-account key is
created or stored. Only this repository can use it, and pull requests never get it, so a fork cannot
spend the query budget. This setup is in place; the commands are what was run.

```bash
PROJECT_ID=vigil-md-1792696816
PROJECT_NUMBER=292904548224
REPO=NainicaD/Vigil-MD
SA=github-ci-trainer@$PROJECT_ID.iam.gserviceaccount.com
```

**1. A service account that can run queries and read two datasets, nothing else:**

```bash
gcloud iam service-accounts create github-ci-trainer --project "$PROJECT_ID" --display-name "GitHub Actions matching training"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" --role roles/bigquery.jobUser --condition=None
for D in vigil_md_model vigil_md_prod; do
  bq query --use_legacy_sql=false "GRANT \`roles/bigquery.dataViewer\` ON SCHEMA \`$PROJECT_ID.$D\` TO \"serviceAccount:$SA\""
done
```

`bq add-iam-policy-binding -d` is refused on this project ("requires allowlisting"), hence the SQL
`GRANT`. The training chain never writes to BigQuery, so no write role is needed.

**2. Writes outside BigQuery, each limited to what the job needs:**

```bash
# s06 (with MLflow) copies the training table to gs://vigil-md-raw-nd/mlflow/data/: only that prefix
for R in objectCreator objectViewer; do
  gcloud storage buckets add-iam-policy-binding gs://vigil-md-raw-nd --member "serviceAccount:$SA" --role roles/storage.$R \
    --condition='expression=resource.name.startsWith("projects/_/buckets/vigil-md-raw-nd/objects/mlflow/data/"),title=mlflow-data-only'
done
# the publish step (s11) uploads to Artifact Registry
gcloud artifacts repositories add-iam-policy-binding vigil-md-models --project "$PROJECT_ID" --location us-central1 \
  --member "serviceAccount:$SA" --role roles/artifactregistry.writer
```

**3. Let this repository act as the service account.** The project already has the workload
identity pool `github` with the provider `vigil-md-repo` (made for `chatbot-publish.yml`; it trusts
only `NainicaD/Vigil-MD`). It is reused, not recreated:

```bash
gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT_ID" --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
```

**4. Three repository variables** (GitHub → repository Settings → Secrets and variables → Actions →
**Variables** tab; they are identifiers, not secrets):

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | `vigil-md-1792696816` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/292904548224/locations/global/workloadIdentityPools/github/providers/vigil-md-repo` |
| `GCP_SERVICE_ACCOUNT` | `github-ci-trainer@vigil-md-1792696816.iam.gserviceaccount.com` |

**Running it.** GitHub shows **Run workflow** only for workflows on the default branch (`main`).
This file is only on `model-dev-integration`, so manual runs (and the `trials` and `refresh_data`
inputs) are not available. Training runs on a push to `model-dev-integration` that touches
`Model-Development/matching/`, and a push run publishes the next patch version when the gate passes.

## 4. Cost

- `s03` downloads the label set through the row-listing API: **no query cost**.
- `s04` runs two small queries, **about 0.3 GB the first time**. Its answers and the downloaded label
  set are cached in GitHub Actions, keyed on `labels/label_set_v1.manifest.json` and `s04`'s code. Later
  runs reuse them and **query nothing** until the label set is rebuilt or `s04` changes.
- Every `s04` call is dry-run first and refused above `--max-gb 2` (the job fails with exit 3).
- `refresh_data` on a manual run ignores the cache and re-queries.
- GitHub Actions minutes: a training run takes 20-60 minutes. Lower `trials` on a manual run for a
  quicker check.

## 5. Running the same thing locally

```bash
python ci/make_ci_feature_table.py --out data/ci/features.csv          # the smoke run
python scripts/s05_build_hand_features.py
python scripts/s06_train_models.py --data data/ci/features.csv --trials 1 --no-mlflow
python ci/check_training_outputs.py
python eval/s08_all.py --exp none --ie-gate --hand-features data/hand_features.csv --no-mlflow
python ci/gate.py --report-only
```

These overwrite `data/hand_features.csv`, `data/scores/` and `data/cutoffs.json`. The same chain in a
container: see the `Dockerfile` (it runs `ci/train_chain.sh`).

The real chain is in the Model-Development README ("The training chain"). To gate an experiment:
`python ci/gate.py --scores-dir data/runs/<exp>/scores` after `eval/s08_all.py --exp <exp>`.

## 6. Changing it

- A new `feat_*` column: add it to `ci/make_ci_feature_table.py`, or the smoke job stops with
  "FEATURE_COLUMNS has columns this generator does not know".
- A new model in `s06`: add its name to `EXPECTED_MODELS` in `ci/check_training_outputs.py`.
- `tests/test_ci_training.py` checks the workflow itself: triggers, that only `train` can reach GCP,
  the order of the stages, the cost guards, the cache key, the uploaded files, and that publishing
  comes after the gate. `tests/test_gate.py` checks the gate, publishing and the IPW weights.
- The gate rules (`ci/gate.json`): change them only before looking at a new result, and write the date
  in `_comment`. A new champion: `python ci/gate.py --scores-dir <its scores> --write-champion --source "<release>"`.
