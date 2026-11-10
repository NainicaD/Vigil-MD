# Model-Development

Two workstreams. Each is self-contained: its own `requirements.txt`, tests and README.

| Folder | What | Owners | Run it |
|---|---|---|---|
| [`matching/`](matching/README.md) | MAUDE -> GUDID same-device matching: exact rules + ML | 3 people | `cd matching && python -m pytest`; stages in `matching/README.md` |
| [`chatbot/`](chatbot/README.md) | Chatbot | 3 people | not started |

Data comes from `../Data-Pipeline/`. Layout inside each workstream: `docs/ sql/ scripts/ eval/ reports/ tests/`
(matching also has `vigil_model/` the shared package, `labels/`, `ci/`).
