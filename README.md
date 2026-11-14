# Vigil MD

**Linking FDA medical device records across databases, with machine learning.**

The FDA keeps separate databases for device recalls, adverse event reports and device
identifiers. The same device or manufacturer is often written differently in each one. Vigil MD
brings these records together so that a researcher can look up the full recall and report
history of a device or a company in one place.

Vigil MD is a course project (Northeastern University, Khoury College, MLOps). It is not a medical tool.

## The four parts

| Part | What it does | Start here |
|---|---|---|
| **Data pipeline** | Downloads four FDA sources every day, checks them, and loads them into a BigQuery warehouse (26.9 million records). Runs on Airflow. | [`Data-Pipeline/README.md`](Data-Pipeline/README.md) |
| **Matching model** | Decides whether an adverse event report and a device record describe the same device, for the reports that carry no device identifier. | [`Model-Development/matching/README.md`](Model-Development/matching/README.md) |
| **Chatbot** | Answers questions about a device or a company from the FDA data, and politely declines medical advice. | [`Model-Development/chatbot/README.md`](Model-Development/chatbot/README.md) |
| **Website** | Device search, a page per device, and the chatbot in a side panel. | [`Web-App/frontend/README.md`](Web-App/frontend/README.md) |

## How they fit together

```
 FDA sources ──▶ Data pipeline ──▶ BigQuery warehouse ──┬──▶ Matching model ──▶ device links
 (openFDA,        (Airflow: fetch,   (raw → staging → prod) │
  AccessGUDID)     check, load)                             └──▶ Chatbot + website ──▶ Cloud Run
```

## What runs automatically

Every change pushed to GitHub is tested. Changes to a model also retrain it, check it, and
publish it only if it is at least as good as the current one.

| Workflow | When | What it does |
|---|---|---|
| [`tests.yml`](.github/workflows/tests.yml) | Every pull request and every push to `main` | Runs the data pipeline and chatbot tests, checks the chatbot model, and builds the website |
| [`model-training.yml`](.github/workflows/model-training.yml) | Changes to the matching model | Trains on the warehouse data, checks accuracy and fairness, and publishes a new version |
| [`chatbot-publish.yml`](.github/workflows/chatbot-publish.yml) | Changes to the chatbot or the website | Retrains and checks the chatbot model, publishes it, and deploys the website; the new version goes live only after a health check |

GitHub signs in to Google Cloud without any stored password or key.

## Where things live

| What | Where |
|---|---|
| Code, tests and documentation | This repository |
| FDA data | Google Cloud Storage (original files) and BigQuery (warehouse) |
| Experiment history | MLflow ([`Model-Development/MLFLOW.md`](Model-Development/MLFLOW.md)) |
| Approved models | Google Cloud Artifact Registry, repository `vigil-md-models` |
| Website and API | Google Cloud Run, service `vigil-md` (private: project members only) |

## Folders

| Folder | Contents |
|---|---|
| `Data-Pipeline/` | Airflow DAGs, scripts, tests, data checks, bias analysis, DVC |
| `Model-Development/` | The matching model and the chatbot ([overview](Model-Development/README.md)) |
| `Web-App/frontend/` | The website |
| `.github/workflows/` | The automated workflows above |
| `docker-compose.yaml`, `docker-compose.elk.yml`, `docker-compose.mlflow.yml` | Airflow, logging (ELK) and the MLflow server, run with Docker |

## Team

Nainica Dasari, Satwik Golla, Abhishek Pramanik, Nandita Mahendra, Hitika Dalwadi, Banu Teja Maram.

## Disclaimer

Vigil MD is for research and education. FDA adverse event data is not validated, and a report
alone does not show that a device caused a problem. For decisions about medical devices, always
use official FDA sources and talk to a clinician.
