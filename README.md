# Vigil MD

ML-based entity resolution across FDA medical device databases.

The data pipeline (acquisition, BigQuery warehouse, validation, anomaly checks, bias analysis,
Airflow DAGs, DVC, tests) is in [`Data-Pipeline/`](Data-Pipeline/). Start with its
[README and Quickstart](Data-Pipeline/README.md).

| Folder | Contents |
|---|---|
| `Data-Pipeline/` | The data pipeline: `dags/`, `scripts/`, `tests/`, `data/`, `logs/`, `docs/`, `reports/`, `dvc.yaml` |
| `Model-Development/chatbot/` | Chatbot models: the question classifier (`question_classifier/`, `tests/`) |
| `.github/workflows/` | CI: both folders' test suites on every pull request |
| `docker-compose.yaml`, `docker-compose.elk.yml` | Airflow and ELK (run from this folder or from `Data-Pipeline/`) |
