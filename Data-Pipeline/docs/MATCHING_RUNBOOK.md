# Matching model on prod: runbook

This runbook is for the person who runs the matching pipeline on `vigil_md_prod` on their own laptop.
The pipeline links MAUDE reports that have no usable UDI to GUDID devices. The chatbot and the website then count these reports for the device, and say that the matching model linked them.

## What runs

| DAG | When | What it does |
|---|---|---|
| `vigil_md_load` | after each daily acquisition | loads the FDA data, then starts `vigil_md_matching` (task `trigger_matching`) |
| `vigil_md_matching` | started by `vigil_md_load`, or by hand | `candidates` → `score` → `load_links` → `build_summaries` on `vigil_md_prod` |
| `vigil_md_matching_check` | every Monday 07:00 UTC | the UDI agreement check: precision and recall on MAUDE rows whose device is known from their UDI |

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

New DAGs start paused. In the Airflow UI (http://localhost:8080), or:
```bash
docker compose exec airflow-worker airflow dags unpause vigil_md_matching
docker compose exec airflow-worker airflow dags unpause vigil_md_matching_check
```
`vigil_md_acquisition` and `vigil_md_load` have their own setup (`Data-Pipeline/README.md`).

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
| `candidates` (BigQuery + download) | 10 to 30 min, about 3 GB billed |
| `score` with 2 workers | about 1.5 to 2 h |
| Memory | about 1.5 GB per worker (one chunk each) |
| `build_summaries` | about 11 GB billed |

If `score` stops (crash, restart), Airflow retries it 2 times. Each retry continues from the last saved chunk. The task log shows `resumed from checkpoint`.

## 8. Daily runs

After the backfill, `vigil_md_load` starts `vigil_md_matching` after each load. Each run scores only the MAUDE rows that changed. With no changed rows, `candidates` is skipped and the run ends.

A new bundle or gate makes the next run score every row again automatically: `load_model_links.py` writes the table label `model_version`, and `s13` compares it.

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
| UDI precision alert | precision below 0.95 in the weekly check | expected with v1.1.0 without the M1 gate; see EXPERIMENTS.md M1 |

## Known limits

- The UDI check is optimistic: rows with a UDI are easier than the rows the model is for.
- The M1 gate is an override until the next bundle release.
- A changed GUDID record does not cause old MAUDE rows to be scored again. A MAUDE row that gets an exact UDI link keeps its old model link. Run `full=true` from time to time (for example after a GUDID release).
  Future fix: in `sql/scoring/01_candidates.sql`, move the `since` filter from the pool to the end, and select the pool rows that changed plus the pool rows with a candidate in a GUDID group with a changed record (`gudid.last_updated_at > @since`). Score all candidates of these rows again, so the delete-and-insert in `load_model_links.py` stays correct. Give the load step the changed MAUDE rows that left the pool, and delete their model links.
- Metricbeat reads the Docker socket, which gives full control of Docker. Use this setup on a local machine only.
