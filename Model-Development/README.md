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

CI: `.github/workflows/tests.yml` (pipeline, chatbot tests and model gate, website) and
`.github/workflows/model-training.yml` (matching: offline tests and a smoke training on every pull
request; real training on BigQuery only on pushes to `main` or `same-device-profiling`).
