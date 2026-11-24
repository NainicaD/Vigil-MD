# Matching model on prod: runbook

This runbook is for the person who runs the matching pipeline on `vigil_md_prod` on their own laptop.
The pipeline links MAUDE reports that have no usable UDI to GUDID devices. The chatbot and the website then count these reports for the device, and say that the matching model linked them.

## What runs

| DAG | When | What it does |
|---|---|---|
| `vigil_md_load` | after each daily acquisition | loads the FDA data, then starts `vigil_md_matching` (task `trigger_matching`) |
| `vigil_md_matching` | started by `vigil_md_load`, or by hand | `candidates` → `score` → `load_links` → `build_summaries` on `vigil_md_prod` (summaries include model links only once switched on, section 7b); `check_drift` after `score` (section 8b) |
| `vigil_md_matching_check` | every Monday 07:00 UTC | the UDI agreement check: precision and recall on MAUDE rows whose device is known from their UDI; then `decide_retrain` (section 8b) |

A daily run scores only the MAUDE rows that changed since the last load. The first run on prod scores every row (the backfill, step 7).

## 1. Laptop

- WSL memory: 12 GB. In `C:\Users\<you>\.wslconfig`:
  ```
  [wsl2]
  memory=12GB
  ```
  Then run `wsl --shutdown` in Windows, and start Docker Desktop again.
- Disk: 20 GB free (the backfill writes about 5 GB in `Data-Pipeline/logs/matching/`).

## 2. Code and credentials

```bash
git clone <repo> Vigil-MD && cd Vigil-MD
git checkout model-dev/batch-scorer        # until it is merged
cp .env.example .env
gcloud auth application-default login      # BigQuery and the BigQuery Storage Read API
```

In `.env`, set these values (see the comments in `.env.example`):
- `_PIP_ADDITIONAL_REQUIREMENTS`: keep the line from `.env.example`. It includes `xgboost` and `scikit-learn`.
- `VIGIL_LOGSTASH_HOST=host.docker.internal`
- `KIBANA_ENCRYPTION_KEY=` the output of `openssl rand -hex 32`. Keep this value: a new key makes the alert rules unreadable.

## 3. The model bundle

Download the released bundle one time. The DAGs read it from this folder:

```bash
mkdir -p Model-Development/matching/models/matching-same-device-v1.1.0
gcloud artifacts generic download --package matching-same-device --version v1.1.0 \
  --repository vigil-md-models --location us-central1 --project vigil-md-1792696816 \
  --destination Model-Development/matching/models/matching-same-device-v1.1.0
```

The folder must hold `meta.json`, `model.json`, `tfidf.pkl`, `manifest.json` and `README.txt`. Git ignores this folder. `tfidf.pkl` is a pickle file: use only bundles from this registry.

The DAGs apply the IE gate `e6_e3b_c1_m1` on top of v1.1.0 (param `ie_gate`). This gate removes a known error: links to another size of the same product family when the report has no model number (`Model-Development/matching/docs/EXPERIMENTS.md`, row M1). When a bundle that names this gate is released, download it, change `MATCHING_BUNDLE` in `Data-Pipeline/dags/vigil_checks.py`, and remove `MATCHING_IE_GATE`.

## 4. Start the containers

```bash
docker compose up -d                                   # Airflow (airflow-init also creates the pools)
docker compose -f docker-compose.elk.yml up -d         # Elasticsearch, Logstash, Kibana, Filebeat, Metricbeat
python3 elk/kibana/setup_kibana.py                     # data views, dashboard, alert rules
```

Check:
```bash
docker compose exec airflow-worker airflow pools list                  # vigil_warehouse and vigil_upload
docker compose exec airflow-worker airflow dags list-import-errors     # "No data found"
```

## 5. Unpause the DAGs

**Run the DAGs on one laptop only.** The daily runs are on Nainica's laptop. On every other laptop, keep all DAGs paused: two laptops would write the same prod tables (`match_candidates_vigil_md_prod`, `link_maude_gudid_model`) and pay for every run twice.

On the laptop that runs them: new DAGs start paused, except `vigil_md_matching_check`, which starts unpaused and runs every Monday 07:00 UTC. In the Airflow UI (http://localhost:8080), or:
```bash
docker compose exec airflow-worker airflow dags unpause vigil_md_matching
```
`vigil_md_acquisition` and `vigil_md_load` start the daily runs. They have their own setup (`Data-Pipeline/README.md`).

On a laptop that must not run them, check that all DAGs are paused, `vigil_md_matching_check` too:
```bash
docker compose exec airflow-worker airflow dags list
docker compose exec airflow-worker airflow dags pause vigil_md_matching_check
```

## 6. Optional: price the first run (free)

```bash
python3 Model-Development/matching/scripts/s13_build_candidates.py \
  --project vigil-md-1792696816 --dataset vigil_md_prod --full --estimate-only
```
Expected: about 2.9 GB for the candidate query.

## 7. First run: the backfill

```bash
docker compose exec airflow-worker airflow dags trigger vigil_md_matching \
  --conf '{"dataset": "vigil_md_prod", "full": true, "workers": 2}'
```

Estimates (the dev run, scaled to prod):

| | Estimate |
|---|---|
| Candidate pairs | about 37 million |
| `candidates` (BigQuery + download) | 10 to 30 min; candidate query 2.85 GB (dry run, 2026-11-23), plus the sibling-key query (cannot be priced before the candidate table exists; capped by `--max-gb 10`) |
| `score` with 2 workers | about 1.5 to 2 h |
| Memory | about 1.5 GB per worker (one chunk each) |
| `load_links` | capped at 5 GB (`load_model_links.py --max-gb`) |
| `build_summaries` | nothing on prod while the switch is off (section 7b) |

If `score` stops (crash, restart), Airflow retries it 2 times. Each retry continues from the last saved chunk. The task log shows `resumed from checkpoint`.

## 7b. Showing model links on the live site (the switch)

The summary tables include model links only for the datasets in `MODEL_LINK_SUMMARY_DATASETS` (`Data-Pipeline/dags/vigil_checks.py`). It holds `vigil_md_dev` only. On prod, the backfill and the daily runs load the links into `link_maude_gudid_model`, but the chatbot and the website still count devices by UDI only, as before.

Why: chatbot code older than this branch says "N reports linked by UDI" for every report of a device. With model links in the summaries, that would be false. So prod is switched on only after the chatbot that labels model links ("... with this device's UDI, ... matched by device details") is deployed.

Status on 2026-11-24: the prod backfill is done (5,278,191 links, one per reported device; UDI check precision 0.941). The chatbot that labels model links is on `model-dev/batch-scorer` only, not on `main`; the live chatbot was last deployed from `main` on 2026-11-18. So prod stays UDI-only.

To switch prod on (after the backfill and that deploy):
0. Check the deploy: the branch is merged into `main`, and the latest `chatbot-publish.yml` run on `main` succeeded after the merge (`gh run list --workflow chatbot-publish.yml -L 3`). Do not go on before this.
1. Add `"vigil_md_prod"` to `MODEL_LINK_SUMMARY_DATASETS` in a pull request.
2. Rebuild the prod summaries once. `--if-changed` may find nothing to do, because a daily build after the last link load is newer than the link table. Price it first; about 11 GB:
   ```bash
   python scripts/warehouse/build_summaries.py --project vigil-md-1792696816 --model-links --estimate-only
   python scripts/warehouse/build_summaries.py --project vigil-md-1792696816 --model-links
   ```
   On the first estimate after this branch is merged, `entity_counts` shows `n/a`: it reads `entity_reports.link_method`, which exists only after `entity_reports` is rebuilt. The build itself prices and builds it after `entity_reports`. On 2026-11-23 the other five tables priced at 8.4 GB on prod; `entity_counts` was 2.4 GB before.
3. Rebuild the chatbot's search index and redeploy, so search counts match.

To check the wording on dev first: `build_summaries.py --project vigil-md-1792696816 --dataset vigil_md_dev --model-links`, then ask the chatbot (on `vigil_md_dev`) about a device with model links.

## 8. Daily runs

After the backfill, `vigil_md_load` starts `vigil_md_matching` after each load. Each run scores only the MAUDE rows that changed. With no changed rows, `candidates` is skipped and the run ends.

A new bundle or gate makes the next run score every row again automatically: `load_model_links.py` writes the table label `model_version`, and `s13` compares it.

**Drift.** Each run compares its score distribution with the reference `Model-Development/matching/reports/score_baseline.json` (the prod backfill's `report.json`; `MATCHING_BASELINE` in `vigil_checks.py`) and records `score_psi` (Kibana alert above 0.2). `s12` skips it, with `score_psi_skipped` saying why, for a run of fewer than 1,000 pairs (chance alone moves the PSI of small runs) or for a run of another bundle or gate. Without the file, runs score as before, without `score_psi`. After a new bundle's first full run, replace the file with that run's `report.json`.

## 8b. Automatic retraining

Two tasks decide whether the model is retrained:
- `decide_retrain`, at the end of the weekly check: UDI precision below 0.90 (param `retrain_precision`).
- `check_drift`, after `score` in each matching run: `score_psi` above 0.2. Runs with fewer than 1,000 pairs, or without a baseline, never trigger.

The levels are the same as the Kibana alerts. When one is crossed, the task starts the GitHub workflow `model-training.yml` on the branch `retrain_ref` (default `main`), with inputs `reason` and `publish=true`. The workflow retrains, runs the model gate and publishes a new bundle only if the gate passes. The task sends a "Retraining started" e-mail (`VIGIL_ALERT_EMAIL_TO`) and the ELK record `matching_retrain_triggered` (dashboard panel "Retraining decisions").

At most one retraining is started per 7 days: if a manual (`workflow_dispatch`) run of `model-training.yml` started in the last 7 days, by the DAGs or by hand, the task records `outcome: cooldown` and starts nothing. These two tasks have no retry, because a retry could start the workflow twice.

Setup, once: create a fine-grained GitHub token (GitHub → Settings → Developer settings → Fine-grained tokens) for this repository only, with the permission "Actions: read and write". Put it in `.env` as `VIGIL_GITHUB_TOKEN` and recreate the containers (`docker compose up -d`). Without the token everything runs as before, and a needed retrain fails its task, so the failure e-mail goes out.

Test or demo (on `vigil_md_dev`, without a real drop): trigger `vigil_md_matching_check` with conf `{"dataset": "vigil_md_dev", "retrain_precision": 0.99, "retrain_ref": "<your branch>"}`. Until the workflow with the `reason` and `publish` inputs is on `main`, GitHub refuses a start on `main` (HTTP 422), so set `retrain_ref` to the branch that has it.

## 9. Where to look

| What | Where |
|---|---|
| Task state, task logs | Airflow, http://localhost:8080 |
| Run numbers, chunks, failures, drift, containers, alerts | Kibana dashboard "Vigil MD matching", http://localhost:5601 |
| Run files (candidates, links, report, checkpoints) | `Data-Pipeline/logs/matching/<run_id>/` |
| The links | BigQuery `vigil_md_prod.link_maude_gudid_model`, view `vigil_md_prod.links` |
| The chatbot counts | `vigil_md_prod.entity_reports` (`link_method`), `entity_counts` (`model_reports`) |

## 10. Problems

| Symptom | Cause | Fix |
|---|---|---|
| A task stays "scheduled" | a pool is missing, or the DAG is paused | step 4 check; unpause the DAG |
| No records in Kibana | `VIGIL_LOGSTASH_HOST` is not set, or ELK is stopped | step 2, step 4 |
| `score` is killed (exit 137) | not enough memory | `workers=1`, or more WSL memory (step 1) |
| "checkpoints of other input" | the run folder has parts from another candidate file, batch, bundle or gate | delete `Data-Pipeline/logs/matching/<run_id>/parts` |
| UDI precision warning | precision 0.90 to 0.95 in the weekly check | prod was 0.941 on 2026-11-24 (one link per device, gate M1): UDI vs model-number disagreements (mostly Dexcom) and missed candidates. Watch the trend |
| UDI precision alert | precision below 0.90 in the weekly check | something changed: compare with the last check report; see EXPERIMENTS.md M1 and the gate param `ie_gate`. `decide_retrain` starts retraining (section 8b) |
| `decide_retrain` or `check_drift` failed: "VIGIL_GITHUB_TOKEN is not set" | a retrain was needed, no token | add the token (section 8b), or start `model-training` by hand in GitHub Actions |
| … failed: "HTTP 401" / "HTTP 403" | the token is wrong, expired, or lacks "Actions: read and write" | make a new token (section 8b) |
| … failed: "HTTP 422" | the branch `retrain_ref` has no `model-training.yml` with the inputs `reason` and `publish` | use a branch that has them (section 8b) |

## Known limits

- The UDI check is optimistic: rows with a UDI are easier than the rows the model is for.
- The M1 gate is an override until the next bundle release.
- A changed GUDID record does not cause old MAUDE rows to be scored again. A MAUDE row that gets an exact UDI link keeps its old model link. Run `full=true` from time to time (for example after a GUDID release).
  Future fix: in `sql/scoring/01_candidates.sql`, move the `since` filter from the pool to the end, and select the pool rows that changed plus the pool rows with a candidate in a GUDID group with a changed record (`gudid.last_updated_at > @since`). Score all candidates of these rows again, so the delete-and-insert in `load_model_links.py` stays correct. Give the load step the changed MAUDE rows that left the pool, and delete their model links.
- Metricbeat reads the Docker socket, which gives full control of Docker. Use this setup on a local machine only.
