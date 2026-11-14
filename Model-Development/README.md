# Model-Development

Two workstreams. Each is self-contained: its own `requirements.txt`, tests and README.

| Folder | What | Owners | Run it |
|---|---|---|---|
| [`matching/`](matching/README.md) | MAUDE -> GUDID same-device matching: exact rules + ML | 3 people | `cd matching && python -m pytest`; stages in `matching/README.md` |
| [`chatbot/`](chatbot/README.md) | Chatbot v1: query router with medical-advice guardrails, device search, cost-capped query layer, template answers, HTTP API and Docker image (no LLM) | 3 people | `cd chatbot && python -m pytest`; API and deploy in `chatbot/README.md` |

Data comes from `../Data-Pipeline/`. Experiment tracking conventions for both workstreams (store,
tags, metric names, registry aliases): [`MLFLOW.md`](MLFLOW.md). The website that uses the chatbot is
in `../Web-App/frontend/`.

Matching layout: `docs/ sql/ scripts/ eval/ reports/ tests/`, plus `vigil_model/` (the shared
package), `labels/`, `ci/`. Chatbot layout: `query_router/ device_search/ query_layer/ answer/
api/ scripts/ tests/ data/` (`chatbot/README.md`).

CI (GitHub Actions):

| Workflow | Runs on | What it does |
|---|---|---|
| `.github/workflows/tests.yml` | Every pull request and push to `main` | Pipeline and chatbot tests, the chatbot model gate, the website build |
| `.github/workflows/model-training.yml` | Changes under `matching/` | Offline tests and a smoke training on every pull request; on a push to `main`, `model-dev-integration` or `same-device-profiling`, real training on BigQuery, the model gate, and publishing to Artifact Registry (`vigil-md-models`) |
| `.github/workflows/chatbot-publish.yml` | Pushes to `model-dev-integration` that change the chatbot or the website | Tests, retraining and the gate, publishing the router to Artifact Registry, then a Cloud Run deploy that gets traffic only after a health check |
