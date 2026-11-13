# Model-Development / matching

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
never run directly. `scripts/` is the entry points, numbered in the order they run.

| Path | What it holds |
|---|---|
| `vigil_model/same_device_features.py` | **The rules.** `pair_features`, the key and placeholder rules, the SQL macros. The only place a rule may be written |
| `vigil_model/bq_utils.py` | Read-only guards, dry-run cost estimate, SQL loading and macro expansion |
| `vigil_model/rows.py` | Table cell -> `DeviceSide` values, shared by `s04` and `s05` so the two feature tables cannot drift |
| `vigil_model/golden_fixture.py` | Proves Python and BigQuery apply the same rules |
| `vigil_model/predict.py` | Loads a released bundle and decides MATCH / NON_MATCH / INSUFFICIENT_EVIDENCE, with the IE gate version the bundle names |
| `vigil_model/ipw.py` | The data pipeline's IPW bias weights mapped onto training rows (`s06 --sample-weight`) |
| `scripts/s01_build_label_set.py` | Runs `sql/labels/*.sql`, writes the label tables to `vigil_md_model` |
| `scripts/s02_build_hand_label_sheet.py` | Draws the hand-label sample, writes `labels/hand_label_sample_v1.csv` |
| `scripts/s03_export_label_set.py` | Downloads a label set (`--table`; the release uses `label_set_v2`) locally. Free: uses the row-listing API, not a query |
| `scripts/s04_build_feature_table.py` | **Label set -> the `feat_*` training table.** The only training stage that needs a billed query |
| `scripts/s05_build_hand_features.py` | The same features for the hand-label sheet |
| `scripts/s06_train_models.py` | Logistic regression + 3 XGBoost variants; cutoff picked on validation, then scored on the test split and the hand sheet. Also writes `trials.csv` and `train_metrics.json` |
| `scripts/s11_package_final.py` | Packages one trained model as a release bundle and publishes it to Artifact Registry |
| `eval/s07_rules_baseline.py` | The guide's decision tree, no training. The bar a model must beat |
| `eval/s08_evaluate_hand.py` | Precision and recall on the hand labels, weighted, with a 95% lower bound and per-slice results. `--out-json` for the gate |
| `eval/s08_all.py` | `s08` for every model of one run |
| `eval/s09_evaluate.py` | The generic scorer, for any labels + scores pair |
| `eval/s10_explain_model.py` | SHAP values for a trained model |
| `eval/compare_models.py`, `eval/hparam_sensitivity.py` | One chart comparing the models; validation AP against each hyperparameter |
| `ci/gate.py`, `ci/gate.json`, `ci/champion_metrics.json` | **The model gate.** A new model must meet the floors and bias ceilings and be no worse than the champion |
| `ci/publish.py` | Publishing checks for `s11`: gate passed, tree committed, version new, model not identical to the newest version |
| `Dockerfile`, `ci/train_chain.sh` | Training image: s05 → s06 → s08 → comparison → gate |
| `scripts/` (unnumbered) | Side analyses the training chain does not depend on |
| `sql/`, `labels/`, `reports/`, `docs/` | Queries, committed label data, generated results, the written rules |
| `data/` | Local outputs: parquet, features, scores. Git-ignored, regenerable |
| `tests/` | Mocked tests; no test talks to real GCP |

## The training chain

The released recipe (label_set_v2, feature set fs2), the same steps CI runs:

```bash
export VIGIL_EXP=my-run                 # outputs go to data/runs/$VIGIL_EXP/, nothing is overwritten
S04="--table label_set_v2 --data data/label_set_v2.parquet --manifest labels/label_set_v2.manifest.json \
  --out data/label_set_v2_fs2_features.parquet --siblings data/sibling_keys_v2.csv --feature-set fs2"
python scripts/s03_export_label_set.py --project $PROJECT --table label_set_v2 --out data/label_set_v2.parquet  # free
python scripts/s04_build_feature_table.py --project $PROJECT --estimate-only --max-gb 2 $S04   # free dry run
python scripts/s04_build_feature_table.py --project $PROJECT --max-gb 2 $S04                   # 0.62 GiB in CI
python scripts/s05_build_hand_features.py --feature-set fs2 --tfidf data/label_set_v2_fs2_features.tfidf.pkl
python scripts/s06_train_models.py --data data/label_set_v2_fs2_features.parquet \
  --manifest labels/label_set_v2.manifest.json
python eval/s08_all.py --ie-gate --manifest labels/label_set_v2.manifest.json
python ci/gate.py --scores-dir data/runs/$VIGIL_EXP/scores        # exit 1: worse than the champion
```

`s04` needs about 3.5 GB of memory with fs2.

Stages `s01` and `s02` rebuild the label set and the hand-label sheet. They are expensive and
rarely needed; only run them if you know why. `docs/LABELS.md` says what they do and
`docs/TABLES.md` gives the cost of each stage.

## MLflow and GCS

`s06` and `s09` record runs in MLflow. The run index lives in `Model-Development/matching/mlflow.db`;
the training data and the trained models live in GCS, so the rest of the team can pull them.

Start the server once (from the repo root; needs `GCLOUD_CONFIG_DIR` set in `.env`):

```bash
docker compose -f docker-compose.mlflow.yml up -d   # UI at http://localhost:5000
```

Then train against it:

```bash
export MLFLOW_TRACKING_URI=http://localhost:5000
python scripts/s06_train_models.py
```

Without `MLFLOW_TRACKING_URI` the scripts write `mlflow.db` directly and keep artifacts local;
`--no-mlflow` skips logging and the GCS upload entirely.

### What goes where

| What | Path |
|---|---|
| Training data | `gs://vigil-md-raw-nd/mlflow/data/<table>/<sha256[:12]>/` |
| Models | `gs://vigil-md-raw-nd/mlflow/artifacts/<experiment>/<run>/artifacts/` |
| Run index | `Model-Development/matching/mlflow.db` (local, git-ignored) |

Set `VIGIL_MLFLOW_GCS` (script) and `MLFLOW_ARTIFACT_ROOT` (compose) to move the root; keep them
pointing at the same place.

The feature table is named by the sha256 of its own bytes, so a run always names the exact file
it trained on and nothing is ever overwritten. Re-running on unchanged data uploads nothing.
Each run carries the hash and the URI as the tags `training_data_sha256` and `training_data_uri`.

### Pulling a run's data and model

Copy the exact table a run trained on, straight from the tag MLflow shows on that run:

```bash
gcloud storage cp gs://vigil-md-raw-nd/mlflow/data/label_set_v2_fs2_features/<sha12>/*.parquet .
```

Models are also in the MLflow registry as `vigil-md-linkage-<variant>`, one version per run:

```python
import mlflow
model = mlflow.xgboost.load_model("models:/vigil-md-linkage-xgb_all/2")   # or runs:/<run-id>/model
```

Loading from the registry needs `MLFLOW_TRACKING_URI` set and read access to the bucket.

### Using the released model (matching-same-device)

The model for team testing is in Artifact Registry. It uses project `vigil-md-1792696816`, repository `vigil-md-models` (generic, `us-central1`) and package `matching-same-device`.

Each release has three identifiers:

| Where | Identifier | Meaning |
|---|---|---|
| Artifact Registry version | `v1.0.0` | Semantic version. MAJOR: the input/output schema changes. MINOR: new features or architecture. PATCH: routine retraining. |
| Artifact Registry annotation `mlflow` | `mlflow-v<N>` or `gh-run-<id>` | Version N of the MLflow registered model `matching-same-device`. A version CI publishes names its GitHub Actions run instead: that run's artifact (kept 90 days) holds its `mlflow.db`. |
| Artifact Registry annotation `git` | `git-<sha7>` | The commit of the serving code (`vigil_model/predict.py`). |
| MLflow | `matching-same-device` version N | Auto-incremented. Carries the alias `@challenger`, `@champion` or `@baseline`, and the tags `semver` and `git_sha`. |

Generic repositories reject tags (HTTP 501), so the `mlflow` and `git` identifiers are version annotations. See them with
`gcloud artifacts versions describe v1.0.0 ...`.

| Version | MLflow | Git | What |
|---|---|---|---|
| `v1.0.0` | `mlflow-v1` | `git-6afe449` | First release: experiment `final_v1`. The config the experiments recommend (`reports/Vigil_MD_Label_Feature_Experiments.pdf`): label_set_v2, feature set fs2 and xgb_no_match_kind, with the IE gate `e6_e3b` applied before the cutoff. Hand sheet 0.959 (3 FP). |
| `v1.1.0` | `mlflow-v2` (`@challenger`) | `git-6270fda` | **Use this one.** The same model, with IE gate `e6_e3b_c1`: a `contains` hit in the middle of the GUDID identifier with disagreeing product codes is INSUFFICIENT_EVIDENCE (`docs/EXPERIMENTS.md` C1). Hand sheet 1.000 (0 FP), recall 1.000; the rule was written after seeing those FPs, on the only hand set there is, so this is optimistic. |
| `v1.1.1` | `gh-run-43509528612` | `git-4892a91` | Published by CI from `model-dev-integration`. The same `model.json`, `tfidf.pkl`, cutoff and IE gate as v1.1.0: proof that training reproduces. Published before `ci/publish.py` skipped identical models; it is a copy, not a new model. |

`meta.json` names the bundle's IE gate (`ie_gate`), and `predict.load` applies that version, so v1.0.0 keeps its decisions with newer code. A version has these files:
- `model.json`: XGBoost
- `tfidf.pkl`: the F1 encoder s04 fitted on the training GUDID texts
- `meta.json`: cutoff, feature columns, lineage and hand-sheet results
- `manifest.json`: sha256 of each file
- `README.txt`

```bash
gcloud artifacts generic download --project vigil-md-1792696816 --location us-central1 \
  --repository vigil-md-models --package matching-same-device --version v1.1.0 --destination models/v1.1.0
```

```python
from vigil_model.predict import load
bundle = load("models/v1.1.0")
feats = bundle.features(maude_side, gudid_side, ctx)   # DeviceSide, DeviceSide, MatchContext
bundle.decide(pd.DataFrame([feats]))                 # score + MATCH / NON_MATCH / INSUFFICIENT_EVIDENCE
```

How a release is built:

**By CI (the normal way).** A push to `model-dev-integration` that touches `Model-Development/matching/` runs the chain above on the real data, then `ci/gate.py`, then `s11 --semver auto --lineage ci`. If the gate fails, the job fails and nothing is published: the champion stays. Otherwise CI publishes the next PATCH version, unless the model is identical to the newest one. Setup and roles: `docs/CI_CD.md`. **Run workflow** (a manual run) is not available, because the workflow is not on `main`.

**By hand.**
1. Run the training chain above with `VIGIL_EXP=<exp>`, up to and including `ci/gate.py`.
2. Commit everything. `s11` refuses to publish from a tree with uncommitted files, so that the `git-<sha7>` annotation names the code that built the bundle.
3. With the MLflow server running, set `MLFLOW_TRACKING_URI=http://localhost:5000`, so that only the server writes `mlflow.db`.
4. Run `python scripts/s11_package_final.py --exp <exp> --semver <x.y.z>`. It checks that the bundle reproduces s06's hand-sheet scores, and registers the MLflow version with its alias and tags. Then it uploads the bundle and writes the annotations. It refuses when the gate did not pass (pass `--gate` for another `gate.json`), and it skips an existing version or a model identical to the newest version. `--print-only` prints the `gcloud` commands and uploads nothing.
5. When the release must become the gate's reference: `python ci/gate.py --scores-dir <its scores> --write-champion --source "<release>"`, then commit `ci/champion_metrics.json`.

Teammates need `roles/artifactregistry.reader` on the repository. The bundle's `README.txt` lists the limits: this is a test model, not a validated one.

## Before any query, every time

Each BigQuery stage takes the same three flags. Use them in this order:

```bash
python scripts/<stage>.py --project $PROJECT --print-only      # show the SQL, no GCP call
python scripts/<stage>.py --project $PROJECT --estimate-only   # free dry run, prints GB
python scripts/<stage>.py --project $PROJECT                   # the real run
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
`vigil_model.*` and `scripts.*` without any `sys.path` juggling. Tests are mocked by policy: a
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
