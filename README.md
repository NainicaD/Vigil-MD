# Vigil MD

**ML-based entity resolution across FDA medical device databases.**

Vigil MD links medical device records from multiple FDA sources into one unified, evidence-linked database. It identifies when records from different sources describe the same manufacturer, device model, or recall, while keeping genuinely different devices and recalls separate. Users can then search a device's or manufacturer's recall history through a chat interface backed by the unified database.

Inspired by Kramer et al. (2012), *Security and Privacy Qualities of Medical Devices: An Analysis of FDA Postmarket Surveillance*, PLoS ONE.

---

## Data Sources

| Source | URL | Role | Update cadence |
|---|---|---|---|
| FDA Enforcement | https://open.fda.gov/apis/device/enforcement/ | Recall actions and classification | Weekly |
| Device Recall | https://open.fda.gov/apis/device/recall/ | Recall details and corrective actions | Weekly |
| MAUDE | https://open.fda.gov/apis/device/event/ | Adverse event and malfunction reports | Quarterly |
| GUDID | https://accessgudid.nlm.nih.gov/download | Canonical device identifiers | Monthly |

Current scope: all device types, 2016–2026.

---

## Project Structure

```
Vigil-MD/
├── dags/
│   ├── pull_samples_dag.py          # development DAG — 200 sample records
│   └── vigil_md_acquisition.py      # production DAG — full pull to GCS
├── scripts/
│   ├── acquisition/
│   │   ├── pull_samples.py          # pull 200 sample records per source
│   │   ├── fetch_sources.py         # full bulk download → GCS
│   │   ├── upload_samples_to_gcs.py # upload sample NDJSON to GCS
│   │   ├── load_samples_to_bq.py    # load raw samples into BigQuery
│   │   └── load_interim_to_bq.py    # load processed Parquet into BigQuery
│   ├── preprocessing/
│   │   ├── schema_map.py            # unified field mapping for all sources
│   │   └── flatten_to_parquet.py    # normalize and flatten to Parquet
│   └── matching/
│       └── exact_linking.py         # UDI and recall-number exact joins
├── tests/                           # pytest unit tests
├── data/                            # DVC-tracked (not in Git)
│   ├── raw/samples/                 # 200-record sample JSON files
│   └── interim/                     # flattened Parquet + linking outputs
├── config.yaml                      # GCP project, bucket, scope settings
├── docker-compose.yaml              # Airflow with CeleryExecutor
├── .env.example                     # environment variable template
├── dvc.yaml                         # DVC pipeline stages
└── requirements.txt
```

---

## GCP Resources

| Resource | Name |
|---|---|
| Project | vigil-md-1792696816 |
| GCS bucket | vigil-md-raw-nd |
| BigQuery (raw) | vigil_md_raw |
| BigQuery (processed) | vigil_md_processed |

---

## Branch Guide

| Branch | Purpose |
|---|---|
| `main` | Stable code — all scripts |
| `airflow-integration` | All scripts + Docker + both DAGs |
| `airflow-setup` | Teammate's original Docker setup |
| `sample-data-pull` | Original sample pull work |

---

## Setup

**Requirements:** Python 3.9+, Docker, Git, Google Cloud SDK

```bash
# Clone and set up environment
git clone https://github.com/NainicaD/Vigil-MD.git
cd Vigil-MD
git checkout airflow-integration

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install "dvc[gs]"
```

**GCP authentication:**

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project vigil-md-1792696816
gcloud auth application-default set-quota-project vigil-md-1792696816
```

**Pull versioned data with DVC:**

```bash
dvc pull
```

This downloads all sample JSON files and processed Parquet files from GCS. No need to re-run acquisition scripts.

---

## Running the Pipeline

### Option A — Manual (step by step)

```bash
# 1. Pull 200 sample records per source
python scripts/acquisition/pull_samples.py --limit 200

# 2. Flatten and normalize
python scripts/preprocessing/flatten_to_parquet.py

# 3. Exact linking
python scripts/matching/exact_linking.py

# 4. Upload samples to GCS
python scripts/acquisition/upload_samples_to_gcs.py --bucket vigil-md-raw-nd

# 5. Load to BigQuery
python scripts/acquisition/load_interim_to_bq.py \
    --project vigil-md-1792696816 \
    --bucket vigil-md-raw-nd
```

### Option B — Full data pull (2016–2026)

```bash
python scripts/acquisition/fetch_sources.py \
    --bucket vigil-md-raw-nd \
    --start-year 2016 \
    --end-year 2026
```

Streams all FDA sources directly to GCS. Takes 20–40 minutes. Re-running is safe — files already in GCS are skipped automatically. Use `--dry-run` to preview what will be downloaded without downloading anything.

---

## Running Airflow with Docker

```bash
# 1. Create your .env
cp .env.example .env

# 2. Initialize the database (first time only)
docker compose up airflow-init

# 3. Start all services
docker compose up -d

# 4. Open the UI
open http://localhost:8080
# Login: airflow / airflow
```

**DAGs:**

| DAG | Purpose | When to use |
|---|---|---|
| `pull_samples` | Pulls 200 sample records per source | Development and testing |
| `vigil_md_acquisition` | Full pull to GCS with validation | Production |

**Everyday commands:**

```bash
docker compose ps                          # service status
docker compose logs -f airflow-scheduler   # follow scheduler logs
docker compose logs airflow-dag-processor  # DAG import errors
docker compose down                        # stop, keep database
docker compose down -v                     # stop and wipe database
```

**Troubleshooting:**

| Problem | Fix |
|---|---|
| Port 8080 in use | Change `8080:8080` mapping in `docker-compose.yaml` |
| Task fails writing to `data/` | Set `AIRFLOW_UID` in `.env` to your `id -u`, restart |
| DAG missing from UI | Check `docker compose logs airflow-dag-processor` |
| `import requests` fails in task | Add `requests` to `_PIP_ADDITIONAL_REQUIREMENTS` in `.env` |

---

## Data Versioning with DVC

```bash
# After changing data files
dvc add data/raw/samples
dvc add data/interim
dvc push

# Commit the pointer files
git add data/raw/samples.dvc data/interim.dvc
git commit -m "chore: update DVC tracking"
git push
```

---

## Testing

```bash
pytest tests/ -v
```

---

## Roadmap

- [x] Sample data pull (all 4 sources)
- [x] Preprocessing and normalization
- [x] Exact linking — 108 MAUDE–GUDID links confirmed
- [x] GCS and BigQuery integration
- [x] Full bulk download (2016–2026)
- [x] Airflow DAG with Docker
- [x] DVC data versioning
- [ ] Test modules
- [ ] Candidate generation and ML matching
- [ ] Unified BigQuery database with provenance
- [ ] Chat interface for recall history search

---

## Disclaimer

Vigil MD is a course project for research and educational purposes. It is not a medical tool. FDA data from openFDA is unvalidated, and adverse event reports alone do not establish that a device caused an outcome. Always consult a clinician and the official FDA source for decisions about medical devices.
