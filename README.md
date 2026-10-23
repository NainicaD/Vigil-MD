# Vigil MD

**ML-based entity resolution across FDA medical device databases.**

Vigil MD links medical device records from multiple FDA sources into one unified, evidence-linked database. It identifies when records from different sources describe the same manufacturer, device model, or recall, while keeping genuinely different devices and recalls separate. Users can then search a device's or manufacturer's recall history through a chat interface backed by the unified database.

## Motivation

FDA device safety data is spread across separate databases that describe devices inconsistently. The same manufacturer or model can appear under different names, and multi-source analyses typically link these records by hand. Vigil MD builds a reusable, measured linkage layer so this work doesn't have to be repeated for every study.

The project is inspired by Kramer et al. (2012), *Security and Privacy Qualities of Medical Devices: An Analysis of FDA Postmarket Surveillance*, PLoS ONE.

## Data Sources

| Source | Content | Role |
|---|---|---|
| openFDA Device Enforcement | Recall actions, classification, product descriptions | Recall evidence |
| openFDA Device Recall | Recall details and corrective actions | Recall evidence |
| openFDA Device Event (MAUDE) | Adverse event and malfunction reports | Adverse-event context |
| GUDID | Device identifiers, brand, model, manufacturer | Reference device catalog |

The current scope is limited to one device category and a fixed time window.

## Pipeline Overview

1. **Acquisition:** fetch a pinned snapshot from each source
2. **Validation:** check schemas and generate data statistics
3. **Normalization:** map sources to a unified schema; normalize names, model numbers, and dates
4. **Exact linking:** link records that share reliable identifiers (recall numbers, UDIs)
5. **Candidate generation:** create candidate record pairs using blocking rules
6. **Feature engineering:** compute pair features (name similarity, model agreement, missing-field flags)
7. **Scope extraction:** parse affected lots, serials, and software versions from recall text
8. **Anomaly checks and alerts:** flag missing data, duplicates, and schema changes
9. **Slice analysis and labeling export:** check coverage across data slices and export pairs for labeling

The pipeline is orchestrated with Apache Airflow and versioned with DVC.

## Project Structure

```
Data-Pipeline/
├── dags/           # Airflow DAG definitions
├── scripts/
│   ├── acquisition/
│   ├── preprocessing/
│   ├── matching/
│   ├── validation/
│   └── analysis/
├── tests/          # pytest unit tests
├── data/           # DVC-tracked data
├── logs/
├── dvc.yaml
└── README.md
```

## Setup

**Requirements:** Python 3.10+, Docker (for Airflow), Git, DVC

```bash
# Clone the repo
git clone https://github.com/<your-username>/vigil-md.git
cd vigil-md

# Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Set your openFDA API key (free; raises rate limits)
export OPENFDA_API_KEY=<your-key>

# Pull versioned data
dvc pull
```

## Running the Pipeline

```bash
# Start Airflow
docker compose up -d

# Open the Airflow UI at http://localhost:8080 and trigger the DAG:
#   vigil_md_data_pipeline
```

See [Running Airflow with Docker](#running-airflow-with-docker) for first-time setup.

To reproduce outputs without Airflow:

```bash
dvc repro
```

## Running Airflow with Docker

Airflow 3 runs from the repo's `docker-compose.yaml` (CeleryExecutor, Postgres, Redis). It uses the official `apache/airflow` image, so there is no custom image to share. The whole repo is mounted into the containers at `/opt/airflow/Vigil-MD`, and Airflow reads DAGs from `dags/`.

**Requirements:** Docker with Compose v2 (on Windows, Docker Desktop with WSL integration enabled), at least 4 GB RAM for Docker, and port 8080 free. Don't run a local `airflow standalone` alongside it.

**First-time setup**

```bash
git clone https://github.com/<your-username>/vigil-md.git
cd vigil-md

# 1. Create your .env
cp .env.example .env
#    set AIRFLOW_UID to the output of `id -u`

# 2. Initialise the DB and admin user (once)
docker compose up airflow-init

# 3. Start everything
docker compose up -d
```

Open http://localhost:8080 and log in with `airflow` / `airflow` (unless you changed it in `.env`). DAGs are paused when created: unpause the one you want and trigger it. New DAG files appear after about 30 seconds.

**Sample-data DAG:** `pull_samples` pulls enforcement, recall, MAUDE and GUDID samples, one parallel task per source. It takes `limit` (max 200) and `product_code` (default `FRN`) when triggered. Output goes to `data/raw/samples/` in your clone, and each run overwrites the previous files.

**Everyday commands**

```bash
docker compose ps                         # service status
docker compose logs -f airflow-scheduler  # follow logs
docker compose logs airflow-dag-processor # DAG import errors show up here
docker compose down                       # stop, keep the DB
docker compose down -v                    # stop and wipe the DB (clean slate)
```

**Troubleshooting**

| Problem | Fix |
|---|---|
| `permission denied ... docker.sock` | `sudo usermod -aG docker $USER && newgrp docker` |
| Port 8080 already in use | Stop the other Airflow, or change the `8080:8080` mapping under `airflow-apiserver` |
| Task fails with `Permission denied` writing to `data/` | Set `AIRFLOW_UID` in `.env` to your `id -u`, then `docker compose down && docker compose up -d` |
| DAG missing from the UI | Check `docker compose logs airflow-dag-processor` for an import error |
| Tasks fail on `import requests` | Set `_PIP_ADDITIONAL_REQUIREMENTS=requests` in `.env` |

Local-development defaults only: don't expose this setup to a network or use it in production. Never commit `.env`.

## Testing

```bash
pytest tests/
```

## Roadmap

- [ ] Data pipeline (acquisition, normalization, validation, versioning)
- [ ] Labeled benchmark of candidate pairs
- [ ] Entity-resolution models (rules, fuzzy matching, learned matcher)
- [ ] Unified database with source provenance
- [ ] Chat interface for recall history search

## Disclaimer

Vigil MD is a course project for research and educational purposes. It is not a medical tool and should not be used to make decisions about medical care. FDA data from openFDA is unvalidated, and adverse event reports alone do not establish that a device caused an outcome.
