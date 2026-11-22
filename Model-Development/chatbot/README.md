# Vigil MD Chatbot

Answers questions about FDA medical-device reports and recalls from the Vigil MD warehouse. It
never gives medical advice and never judges whether a device is safe: such questions get a fixed
refusal, and their factual part is still answered.

- **No language model writes text.** Every sentence comes from a fixed template filled with
  numbers from BigQuery, so nothing can be invented.
- **One trained model**, the query router, decides what a question asks for; fixed guardrail
  rules back it up on medical advice.
- **Every answer cites its sources** and costs at most 0.5 GB of BigQuery reads.

## Architecture

| Part | Responsibility | Code |
|---|---|---|
| Query router | Which facts a question asks for, and whether it asks for medical advice | [`query_router/`](query_router/) |
| Guardrail rules | Fixed patterns that refuse advice even when the model misses it; fixed reply texts | [`query_router/guardrails.py`](query_router/guardrails.py) |
| Device search | Which device, product code or manufacturer a question names | [`device_search/`](device_search/) |
| Query layer | Fixed, parameterized SQL on the daily summary tables | [`query_layer/`](query_layer/) |
| Answers | One chatbot turn: route, search, query, render from templates | [`answer/`](answer/) |
| API | HTTP API for the website; also serves the website | [`api/app.py`](api/app.py) |
| Website | React front end | [`../../Web-App/frontend/`](../../Web-App/frontend/) |

A question flows: router + rules → device search → summary tables in BigQuery → template answer
with sources.

## Quick start

From `Model-Development/chatbot/`, in the project's Python 3.12 environment:

```bash
pip install -r requirements.txt
python -m query_router.generate                 # synthetic training data: data/generated/
python -m query_router.train                    # router: models/baseline/ + an MLflow run
python -m device_search.build_index             # search index for vigil_md_dev
VIGIL_BQ_DATASET=vigil_md_dev python -m answer "Was the DreamStation recalled, and why?"
VIGIL_BQ_DATASET=vigil_md_dev uvicorn api.app:app --port 8000   # API at localhost:8000/api/docs
pytest -q                                       # unit tests: no network, no cost
```

BigQuery access uses gcloud application-default credentials
(`gcloud auth application-default login`). Use `vigil_md_dev` for development: a question there
bills a few tens of MB.

## Query router

The router reads a question and returns which fixed queries to run, whether part of it must be
refused, and which fixed reply to give. A classifier and the guardrail rules decide together.

```python
from query_router.predict import QueryRouter
router = QueryRouter.load("models/baseline")
router.classify("How many deaths has the t:slim X2 had? Should I stop using it?")
# {"labels": ["counts", "out_of_scope"], "run_queries": ["counts"],
#  "out_of_scope": True, "reply_kind": "medical_refusal",
#  "reply": "I can't give medical advice ...", "refusal": "I can't give medical advice ...",
#  "unclear": False, "rule_hits": ["should_i", "stop_switch"], "scores": {...}}
```

**Labels** (multi-label, defined in [`labels.py`](query_router/labels.py)): `counts`, `problems`,
`recalls`, `narratives`, `identity`, `out_of_scope`. The router does not find the device; device
search does.

### Medical-advice guardrails

Safety is decided in layers:

1. **Rule backstop** ([`guardrails.py`](query_router/guardrails.py)): patterns that fire when
   the user asks the bot to judge or advise: "is X safe", "should I…", "should I stop / switch",
   "which is best for me", "better than", "how much insulin should I take", first-person symptoms
   ("I have a rash", "I noticed a lump", "my dad has swelling"), "would you recommend", "can I
   still use", opinions ("do you think it's trustworthy"), urgent device states ("my CPAP keeps
   alarming"), decisions ("should our hospital switch") and the user's own device ("mine feels
   loose"). A trigger word alone ("dangerous malfunctions reported", "dosing errors") does not fire.
2. **Classifier threshold** for `out_of_scope`, tuned together with the rules for at least 98%
   recall on validation advice questions, capped at 0.5.
3. **Routing**, default policy `facts_with_refusal`: the advice part gets a fixed refusal (never
   model-written; it points to a doctor, 911 and FDA MedWatch) and the factual part is still
   answered. Policy `strict` refuses the whole question.
4. **Template check:** a test rejects judgement or advice words in every answer template.

**Rules are versioned.** `rules_fingerprint()` (currently `5d1584bb53b3`, revision 4) is stored
with every trained model and evaluation; `QueryRouter.load` refuses a model trained under other
rules. After a rule change, run `generate` and `train` again. A rule change is evaluated on a test
set written after the change.

### Fixed replies

Every reply below is fixed text in `guardrails.py`. `classify()` returns which one to show in
`reply_kind` and its text in `reply`.

| `reply_kind` | When | Text starts |
|---|---|---|
| `help` | The whole question asks about the bot ("what can you do", "help"), and no advice rule fires. No query runs. | "I answer questions about FDA reports and recalls for medical devices…" |
| `medical_refusal` | `out_of_scope` with any health or device context: an advice rule, a factual label, a health or device word, or a word with a digit | "I can't give medical advice… If this is an emergency, call 911…" |
| `scope` | `out_of_scope` with none of those signs ("write me a poem") | "I can only answer questions about FDA reports and recalls…" |

When in doubt the reply is `medical_refusal`. These replies change only the text shown, never
which questions are refused.

### Training data

Questions are generated from templates ([`templates.py`](query_router/templates.py)) with
real-world slot values ([`slots.json`](query_router/slots.json)), including 55 medical-advice
phrasings, many of them indirect ("my dad's pump has hundreds of death reports, is that bad?").
Twenty per cent of the questions of every label, advice included, start with a context sentence
("we're reviewing our suppliers this quarter."), so the reason someone gives for asking carries
no label signal.

Three disjoint splits, by device, company and product-code names, by template and by context
sentence:

| Split | Questions | Used for |
|---|---|---|
| train | 4,000 | fitting the model |
| val | 1,000 | choosing C and the per-label thresholds |
| heldout | 1,000 | a check on unseen names and phrasings |

### Evaluation sets

Fixed sets in [`data/test/`](data/test/README.md), written by people who had not seen the rules,
templates or generated data:

| Set | Questions | Used for |
|---|---|---|
| **`handwritten_test_v2.jsonl`** | 242 (67 advice, 14 unrelated, 161 in scope) | **the test set**: the gate, CI and the reported numbers; slice fields audience, device type, event type, style |
| `handwritten_test.jsonl` | 230 | development set (used to write rule revision 3) |
| `false_alarm_set.jsonl` | 42 | factual questions with trigger words: how often they are flagged |
| `e2e_dev.jsonl` | 48 | the whole chatbot on `vigil_md_dev`, with reference answers |
| `e2e_prod.jsonl` | 22 | the whole chatbot on `vigil_md_prod`, devices outside the dev extract |

### Model and selection

The router is TF-IDF on words and characters with one logistic regression per label
([`train.py`](query_router/train.py)). C is chosen on validation, then each label's decision
threshold; the model is registered in MLflow.

**Model comparison** ([`compare.py`](query_router/compare.py)): each model type is chosen on
train and validation, then scored once on the test set. Safety comes first, so logistic
regression is the champion:

| Test set, rules revision 3 | **Logistic regression, C = 1** | Linear SVM, C = 4 | Naive Bayes, alpha = 1 |
|---|---|---|---|
| Advice recall (67) | **1.000** | 0.970 | 0.985 |
| False refusals (161) | 16.2% | 13.0% | 12.4% |
| Normal questions answered | **88.2%** | 82.6% | 79.5% |
| Macro-F1 | 0.761 | 0.761 | 0.736 |

**Tuning** ([`tune.py`](query_router/tune.py)): Optuna (TPE, 30 trials, seed 13) over C, word and
character n-gram ranges, `min_df` and class weighting. Each trial is scored on validation like
`train`; a trial below 98% advice recall loses to every trial that reaches it. The best trial is
registered with the alias `challenger` and compared with the champion by the gate.

### Current champion

MLflow model `chatbot-query-router`, alias `champion`; published as `5d1584bb53b3-eeb32c6`.

| Metric | Test set (242) | Validation (1,000) |
|---|---|---|
| Advice recall (rules + model) | **1.000** | 0.987 |
| False refusals | 13.0% | 8.1% |
| Normal questions answered (`facts_with_refusal`) | 88.2% | |
| Macro-F1 / exact match | 0.770 / 0.616 | 0.797 / 0.621 |
| False-alarm set flagged (42) | 28.6% | |
| Advice recall, rules alone / model alone | 0.642 / 0.970 | |

The champion's numbers are kept in git as
[`champion_metrics.json`](query_router/champion_metrics.json), the reference for CI.

### Bias slices

[`bias.py`](query_router/bias.py) reports, for every audience, device type, event type and
phrasing style: advice recall, false refusals, exact match and the share answered, and the gap
between the best and worst slice (slices with at least 8 questions in that metric). Gap limits:
0.15 for advice recall, 0.25 for false refusals.

| Champion, test set | Advice-recall gap | False-refusal gap (worst vs best) |
|---|---|---|
| audience | 0.00 | 0.183 (procurement vs clinician) |
| device type | 0.00 | 0.173 (hip/knee implant vs none named) |
| event type | 0.00 | 0.185 (none named vs injury) |
| style | 0.00 | 0.335 (long vs short; 8 long questions) |

The context sentences in the training data are the mitigation for long, procurement-style
questions: they lowered the style gap from 0.585 and false refusals from 16.2% to 13.0% at the
same advice recall. The end-to-end evaluation slices answers by device type and event type too.

### Sensitivity to the refusal threshold

[`sensitivity.py`](query_router/sensitivity.py) sweeps the `out_of_scope` threshold with the
rules on, per data set, and writes a CSV, JSON and plot to `reports/sensitivity/` and the MLflow
run.

| Champion, threshold 0.50 | Advice recall | False refusals | Highest threshold keeping 98% advice recall |
|---|---|---|---|
| Validation | 0.987 | 8.1% | 0.55 (5.5% false refusals) |
| Held-out | 0.995 | 23.3% | 0.70 (11.6%) |
| Test set | 1.000 | 13.0% | 0.60 (9.3%) |

The threshold is chosen on validation only; choosing it from the test set would be tuning on it.

### Feature importance

For logistic regression on TF-IDF, each weight is the feature's exact effect on a label's
log-odds, so the top weights are the importance measure. [`importance.py`](query_router/importance.py)
writes the strongest word and character features per label to
`reports/importance/router_features.json` and the MLflow run, and flags device or company names
among them (there are none).

| Label | Words that raise it most |
|---|---|
| counts | "how many", "number of", "total", "reports" |
| problems | "issues", "are reported", "what goes (wrong)", "problem" |
| recalls | "recalls", "class", "why was", "recalled" |
| narratives | "the injury", "reports for", "summarize", "say" |
| identity | "udi", "makes", "who", "what company" |
| out_of_scope | "my", "is", "should", "is it", "good" |

### Commands

```bash
python -m query_router.generate          # data/generated/{train,val,heldout}.jsonl
python -m query_router.train             # models/baseline/ + MLflow run; --alias champion
python -m query_router.evaluate          # held-out + false-alarm set + bias, added to that run
python -m query_router.evaluate --test data/test/handwritten_test_v2.jsonl --prefix test
python -m query_router.sensitivity       # threshold sweep: reports/sensitivity/
python -m query_router.importance        # top features per label: reports/importance/
python -m query_router.tune --trials 30  # Optuna: models/optuna, alias challenger
python -m query_router.compare           # logistic regression vs linear SVM vs naive Bayes
python -m query_router.gate --candidate models/optuna --champion models/baseline   # [--promote]
python -m query_router.publish --model-dir models/baseline --print-only            # registry check
```

## Model lifecycle: MLflow, the gate and Artifact Registry

MLflow records every run and marks the trusted version; the gate decides promotion; Artifact
Registry holds the approved copy that every deployment uses.

1. **Train or tune** (`train.py`, `tune.py`).
2. **MLflow** records the run and registers a new version.
3. **Gate** compares the candidate with the champion on the test set.
4. **Champion:** a pass moves the `champion` alias and rewrites `champion_metrics.json`.
5. **CI** retrains from the commit, runs the gate, and checks that the model equals the champion.
6. **Artifact Registry** stores the approved model as a version with a manifest and checksums.
7. **Docker** builds the image from that version (`MODEL=published`), checking the checksums.
8. **Cloud Run** serves it; `/api/health` reports the version.

### Experiment tracking (MLflow)

Shared conventions: [`../MLFLOW.md`](../MLFLOW.md); code: [`tracking.py`](query_router/tracking.py).

- **Store:** `Model-Development/mlflow/` on each machine (not in git). View it from the repository
  root with `mlflow ui --backend-store-uri sqlite:///Model-Development/mlflow/mlflow.db --port 5050`
  and open http://127.0.0.1:5050 (port 5000 is taken by AirPlay on macOS). Tests always log to a
  temporary store.
- **Experiments:** `chatbot/query-router` (training, tuning, comparison) and `chatbot/answer-v1`
  (end-to-end evaluation). Registered model: `chatbot-query-router`.
- **`train`:** a parent run `tfidf_lr-grid` with one nested run per C, the required tags
  (`workstream`, `git_commit`, `git_dirty`, `data_version`, `author`) plus `rules_fingerprint`, the
  `val_*` metrics, the thresholds and the model files, registered as a new version.
  `meta.json` records `mlflow_run_id` and `model_version`.
- **`tune`:** a parent run `tfidf_lr-optuna` with one nested run per trial and the best model
  (alias `challenger`).
- **`evaluate`:** adds `heldout_*` and `test_*` metrics (advice recall and false refusals for
  rules, model and both; macro-F1; share answered; largest bias gaps) and `falsealarm_flagged*` to
  the same run, with the full report. `sensitivity` and `importance` add their reports.
- **Aliases:** `champion` is the model in use, `challenger` a candidate. Earlier versions stay
  registered, so moving the alias back is a rollback.

### Model gate ([`gate.py`](query_router/gate.py), [`gate.json`](query_router/gate.json))

A candidate replaces the champion only if it:

1. has the same guardrail rules and is scored on the same data;
2. meets the floors and ceilings: advice recall ≥ 0.90, answered ≥ 0.85, false refusals ≤ 0.35,
   false-alarm set flagged ≤ 0.50, bias gaps ≤ 0.15 (advice recall) and ≤ 0.25 (false refusals).
   A ceiling the champion already breaks only warns if the candidate is not worse on it; floors
   are absolute;
3. is not worse than the champion beyond the tolerances (advice recall 0.01, macro-F1 0.02,
   answered 0.02, false refusals 0.02, false-alarm set 0.05);
4. is better by at least 0.01 on macro-F1, false refusals or answered.

`--promote` then moves the `champion` alias and rewrites `champion_metrics.json`.

### Model registry (Artifact Registry, [`publish.py`](query_router/publish.py))

| | |
|---|---|
| Repository | `vigil-md-models`, generic format, `us-central1`, project `vigil-md-1792696816` |
| Package | `chatbot-query-router` |
| Version | `<rules fingerprint>-<commit>`, e.g. `5d1584bb53b3-eeb32c6` |
| Files | `model.joblib`, `meta.json`, `manifest.json` |
| Manifest | package, version, git commit, rules fingerprint, `approved_model_version` (the MLflow version the gate approved), the MLflow run that built the files, test metrics, SHA-256 of each file, publish time |

`publish.py` uploads only when all of these hold:

- the model, scored again on the test set, matches `champion_metrics.json` (within 0.0001) under
  the same rules and data;
- the working tree is clean, so the model can be rebuilt from its commit (a refusal lists the
  files);
- the version does not exist yet (publishing again does nothing).

```bash
python -m query_router.publish --model-dir models/baseline --print-only   # check, no upload
python -m query_router.publish --list                                     # published versions
python -m query_router.publish --download <version> --out models/published  # checks SHA-256
```

Exit codes: 0 published, skipped or downloaded; 1 not the approved model; 2 bad input; 3 gcloud
failed. Every version is kept, so a rollback deploys an older version.

### CI/CD

| Workflow | Runs on | What it does |
|---|---|---|
| `.github/workflows/tests.yml`, job `chatbot-models` | Pull requests and pushes to `main` | Unit tests; retrains the router; `gate --regression-only` against `champion_metrics.json` |
| `.github/workflows/chatbot-publish.yml` | Pushes to `main` that touch the chatbot or the website, or by hand | Unit tests; retrain; gate; approval check; keyless GCP sign-in; publish to Artifact Registry; lists the versions on the run page; keeps the run's MLflow store as artifact `mlflow-ci` (90 days) |

- **Regression mode:** a retrain of the same recipe cannot beat itself, so no gain is needed;
  floors and tolerances fail the build, ceilings show as warnings. A change that makes the router
  worse cannot merge or be published.
- **Keyless sign-in:** Workload Identity Federation (pool `github`, provider `vigil-md-repo`,
  accepting only this repository) lets the workflow act as `vigil-md-ci-publisher`, whose only
  permission is writing to `vigil-md-models`. The repository variables `GCP_WIF_PROVIDER` and
  `GCP_PUBLISH_SA` name them; no key is stored. Without them the workflow stops before publishing
  and says so on the run page.
- **Alerts:** GitHub e-mails the person who pushed when a run fails.

## Query layer

[`query_layer/`](query_layer/) fetches the facts for an answer with fixed, parameterized SQL on
the daily summary tables, built by `Data-Pipeline/scripts/warehouse/build_summaries.py`.

```python
from query_layer import BigQueryBackend, Budget, device_summary, narratives, identity
backend = BigQueryBackend()                 # dataset: VIGIL_BQ_DATASET (default vigil_md_prod)
budget = Budget()                           # one per question (cap 0.5 GB)
device_summary(backend, "device", "dev:f0cdc15b4d63", budget)   # entity, counts, problems, recalls
narratives(backend, "product_code", "OZD", budget=budget, event_types=("Death",))
identity(backend, "device", "dev:f0cdc15b4d63", budget)         # who makes it
```

An **entity** is `(entity_type, entity_id)`, the same in the summary tables and the search index:

| `entity_type` | `entity_id` | Its reports |
|---|---|---|
| `device` | `dev:` + 12 hex (GUDID records grouped by company and brand) | MAUDE rows with one of its UDIs (exact UDI links) |
| `product_code` | the FDA product code, e.g. `LZG` | MAUDE rows with that product code |
| `manufacturer` | `manufacturer_name_norm`, e.g. `respironics` | MAUDE rows with that name |

| Function | Returns | Reads |
|---|---|---|
| `device_summary` | `entity` (title, company, product codes, `found`); `counts` (total, by event type, by year) and `counts_note`; `problems` (top 10 device and patient terms, `no_harm_reports`); `recalls` (total, newest 50 with class, reason, root cause, action) | `entity_catalog`, `entity_counts`, `entity_problems`, `entity_recalls` |
| `narratives` | up to 5 excerpts (about 600 characters), one per event type in turn, newest first; `event_types` keeps only the types a question names (death, injury, malfunction) | `entity_reports`, then `maude_text` for those reports |
| `identity` | a device's GUDID name, company, model numbers, product codes; for a product code or manufacturer, its catalog entry | `device_catalog` or `entity_catalog` |

- **One partition per lookup:** every query filters on `bucket` (`config.entity_bucket`, the same
  function as the pipeline's), `entity_type` and `entity_id`. A device summary reads under 1 MB;
  BigQuery bills at least 10 MB per table, so a typical question bills 40–50 MB on PROD.
- **Safety:** the entity is always a bound parameter, and every statement is checked to be a
  single read-only `SELECT`.
- **Cost guard:** a step's queries are dry-run first (free); if together they would exceed what the
  question has left (`MAX_GB_PER_QUESTION`, 0.5 GB), nothing runs (`QueryBudgetExceeded`). Each
  job's `maximum_bytes_billed` is the budget left minus the other jobs' estimates.
- **Speed:** a step's dry runs and queries run in parallel; each table's last-modified time is
  cached for 5 minutes.
- **Sources:** every result carries the queries run (SQL, parameters, bytes) and the tables'
  last-modified times.
- **Settings:** [`config.py`](query_layer/config.py). `python -m query_layer.estimate
  --entity-type device --entity-id dev:f0cdc15b4d63` prints what each query would read (free).

## Device search

[`device_search/`](device_search/) finds entities from what people type. The website's search box
and the chatbot share it. It runs on a local SQLite file (`data/search/<dataset>.sqlite`, not in
git) built from `entity_catalog` and `device_catalog`; searching costs nothing.

```python
from device_search import DeviceIndex
index = DeviceIndex.open("vigil_md_dev")
index.search("dreamstaton")                    # ranked devices, manufacturers, product codes
r = index.resolve("How many deaths were reported for the dreamstaton?")
r.status       # resolved | several ("X vs Y") | ambiguous (bot asks which) | none (bot asks)
device_summary(backend, **index.query_key(r.matches[0]), budget=budget)
```

**Entries:** every device, product code and manufacturer in `entity_catalog`, with the query
layer's `entity_type` and `entity_id`. Devices also carry their model numbers and UDIs.
[`aliases.json`](device_search/aliases.json) maps names users type to names in the data
(Philips → Respironics).

**Matching:** words in names and model numbers, with typos, 4-letter prefixes and plurals allowed;
a device's company name counts slightly less, so "medtronic" ranks manufacturers first and
"philips dreamstation" finds the device. Candidates come from a trigram index (up to 3,000); words
under 3 letters ("go", "g6") are pre-selected by SQLite. Equal scores rank by report count.

**Resolving a question:**

1. A UDI (8–14 digits) wins, then a known product code in capitals.
2. Device words are the question's words without question, judgement and pronoun words or years;
   parts of hyphenated names ("V-Go") and a capitalised word or one-digit model number after a
   name word ("DreamStation Go", "HeartMate 3") are kept.
3. "X vs Y", "X and Y" and lists resolve each part ("several"). In a comparison (compare, vs,
   versus, both, each, "which has more") whose parts each find something but not one device each,
   the bot asks which one is meant for the unclear part ("DreamStation and t:slim X2": which t:slim
   X2?) instead of searching the joined phrase. A name with "and" in it ("Smith and Nephew") still
   resolves as a whole first; known limit: when the joined phrase resolves ("Impella and breast
   implants" -> one breast implant), it wins.
4. A question with several sentences is answered by the one clause that names a device with all
   its words when the other clauses find nothing, so a sentence about why the user asks ("Our
   contract is up for renewal;") does not hide the device.
5. The top match is taken when it is alone; when the one entry whose whole name was typed ties
   with longer names; when the same device is listed again under its company's name with 3× the
   reports; or when it has 3× the reports of an equal match from the same company and 20× those of
   one from another company. Otherwise the bot shows up to 3 matches and asks. Matches under 0.80
   are not offered.
6. A phrase that does not resolve is retried with only the words that match an entry
   confidently; a word with a digit is never dropped, and the retry counts only if it resolves to
   a device (an unknown model name never turns into its company).

**Report counts for devices are exact UDI links.** Reports without a UDI count under the product
code and manufacturer; predicted links from the matching model will add to device counts through
the summary tables.

```bash
python -m device_search.build_index                                         # vigil_md_dev
python -m device_search.build_index --dataset vigil_md_prod --estimate-only # about 0.15 GB
python -m device_search.build_index --dataset vigil_md_prod                 # 712 MB file
```

| Index | Devices | Manufacturers | Product codes | UDIs |
|---|---|---|---|---|
| `vigil_md_dev` | 838 | 220 | 5 | 8,155 |
| `vigil_md_prod` | 337,729 | 19,519 | 3,805 | 5,258,174 |

Rebuild the index after the summary tables change (daily on PROD).

## Answers (chatbot v1)

[`answer/`](answer/) is one chatbot turn: a question in, an answer with sources out.

```python
from answer import pipeline
bot = pipeline.Chatbot.open("vigil_md_dev")   # router model, search index, BigQuery
out = bot.answer("Was the DreamStation recalled, and why?")
out["text"]       # the answer (Markdown)
out["status"]     # answered | refused | help | scope | unclear | need_device | not_found |
                  # ambiguous | budget | error
out["sections"], out["sources"], out["entities"], out["budget_gb"]
out["template_fingerprint"], out["rules_fingerprint"]
bot.answer("Any deaths?", entity=("device", "dev:f0cdc15b4d63"))   # from a device page
bot.answer("DreamStation", context={"previous_question": "are there any deaths associated to this",
                                    "previous_status": "need_device", "entity": None})
```

- **Order:** the fixed reply (the medical refusal first when facts were also asked), then per
  entity a headline and the sections asked for (identity, counts, problems, recalls,
  narratives), then the disclaimer, the UDI note for devices and the sources.
- **Templates** ([`templates.py`](answer/templates.py)): every sentence in one file;
  `templates_fingerprint()` changes with any wording. Tests reject judgement or advice words
  (with their inflections) in every template and in fully rendered answers. Report text and recall
  reasons are quoted from the FDA as they are.
- **A device with no label:** when the router finds no label and no advice but the question names
  one device (or comes from a device page), the device's summary is shown after a short note.
- **An event word with no label:** a question that names deaths, injuries or malfunctions ("are
  there any deaths associated to this") is a counts question even when no label passed its
  threshold (`rule_hits`: `event_word_counts`): with a device it gets the counts, without one the
  bot asks which device ("dead" is left out: "the pump went dead" is a malfunction).
- **Short-term memory (one turn back):** the browser keeps its last finished turn and sends it as
  `context` (`previous_question`, `previous_status`, and `entity` when that answer had exactly one
  found entity) with a question that has no page entity. The server stores nothing. Rules in
  `Chatbot.answer`, in this order:
  1. An explicit `entity` (device page, "ambiguous" choice) wins; `context` is ignored.
  2. The new question is routed as always: help, scope and refusal replies are final, so context
     never turns a refusal into an answer.
  3. **Pending question:** the previous answer was `need_device` or `unclear`, the new message
     has no label and no event word, and its search resolves exactly one entry: the previous
     question is answered about that entry (routed again, so its refusal still applies).
     `context_used`: `previous_question`. "are there any deaths associated to this" → "Which
     device?" → "DreamStation" gives DreamStation's death counts.
  4. **Carry-over:** the new question names no device (search `none` with an empty phrase) and
     `context` has an entity: that entity is used. `context_used`: `entity`. "what about
     recalls?" after an answer about the DreamStation gives its recalls. A device named in the
     new question wins, and a name that is not found still gets "not found".
  5. Otherwise the question is answered on its own (`context_used`: null).
  The website shows "About: DreamStation (from your previous question)" when `context_used` is
  set; Clear (or a reload) drops the memory. Known limit: rule 3 accepts any single entry the
  search resolves (a device, a company or a product code), as the "Which device?" reply asks for
  any of the three.
- **Report texts over the cost cap:** fewer candidates are read (the 2n, then n newest reports),
  then one event type at a time (deaths, injuries, malfunctions, skipping a type without texts),
  with a note; only if nothing fits, a cost-limit message. Each refused try is a free dry run. A
  report-texts question on a large device bills about 0.35 GB (2026-11-22), a normal question
  about 40 MB.
- **Edge cases**, each a fixed reply: no device named, device not found, several close matches,
  comparisons (one block per device, at most 3, no ranking), no reports, over the cost cap,
  BigQuery unreachable.
- **Writing rules:** "N reports describe…", never a safety judgement; MAUDE counts reports, not
  failure rates; device counts say "linked by UDI"; `(B)(4)` and `(B)(6)` in narratives mean text
  withheld by the FDA.

### End-to-end evaluation

[`answer/evaluate.py`](answer/evaluate.py) runs the reference questions through the whole chatbot
and checks the status, the device named, the refusal and the sections. Results go to MLflow
experiment `chatbot/answer-v1` (router version, rules and template fingerprints, index, cost),
with accuracy per device type, event type and advice/no-advice question.

```bash
python -m answer.evaluate --dataset vigil_md_dev                                    # e2e_dev.jsonl
python -m answer.evaluate --dataset vigil_md_prod --e2e data/test/e2e_prod.jsonl    # PROD index needed
```

On `vigil_md_dev` (48 questions): 87.5% correct (status, device and refusal all right), refusal
placed correctly 97.9%, about 1.2 s per question. `pytest -m integration
tests/test_answer_integration.py` fails below 80%.

## API ([`api/app.py`](api/app.py))

FastAPI around the device search, the query layer and the chatbot.

| Endpoint | Returns | BigQuery |
|---|---|---|
| `GET /api/health` | status, dataset, rules and template fingerprints, index build time, `model_version` (the published router version) | none |
| `GET /api/search?q=<2-200 chars>&limit=<1-25>` | `{query, hits}`; each hit has `id, kind, entity_id, title, subtitle, reports, deaths, injuries, product_codes` | none |
| `GET /api/entity/<kind>/<entity_id>` | `device_summary()` of one entity (device page) | about 40–50 MB billed |
| `POST /api/answer` `{"question": "<1-1000 chars>", "entity": null or {"entity_type", "entity_id"}, "context": optional {"previous_question": "<1-1000 chars>", "previous_status": "<1-40 chars>", "entity": null or {"entity_type", "entity_id"}}}` | `Chatbot.answer()`, plus `context_used` (null, `previous_question` or `entity`; see [short-term memory](#answers-chatbot-v1)) | as needed, capped at 0.5 GB |

- **No SQL leaves the API:** every `sources` list is trimmed to `{query, tables}`.
- **Errors are JSON with a status:** 404 `not_found`, 413 `budget`, 503 `error` (BigQuery
  unreachable), 422 bad input, 429 `rate_limited`.
- **Rate limit:** questions and device pages, 30 a minute per visitor
  (`VIGIL_RATE_LIMIT_PER_MINUTE`); search is not limited. Behind Cloud Run, uvicorn trusts the
  proxy's `X-Forwarded-For` (`--forwarded-allow-ips`), so the limit applies per visitor.
- **Questions are not logged.** Each answer writes one monitoring event with numbers and labels
  only; see [Monitoring events](#monitoring-events-apieventspy).
- **Website:** `VIGIL_WEB_DIST=<dist/>` serves the front end from `/`; any non-API path returns
  `index.html`, so device links work when opened directly; unknown `/api/...` paths stay JSON 404s,
  and nothing outside `dist/` is read. `VIGIL_CORS_ORIGINS=http://localhost:5173` allows the
  front end's dev server.
- **Startup:** the server loads the chatbot (router, search index, BigQuery client) and runs one
  search and one routing before it accepts requests, and logs the step times
  (`"event": "startup"`). Before, this happened on the first request: in the Cloud Run logs
  (10–19 Nov) the first search or answer after a container start took 15–37 s, and two answers
  timed out at 60 s. Cloud Run's default startup check waits for it. `VIGIL_WARM_UP=0` skips it.
  It does not remove the wait when the service starts from zero (min-instances 0); only
  `--min-instances 1` does that (about $10–20 a month; planned for launch).
- **Caches:** BigQuery results are kept until a table they read changes (its last-modified
  time, checked at most every 5 minutes), so a repeated device page or answer costs nothing
  and needs no BigQuery round trip (`VIGIL_RESULT_CACHE`, default 2,000 results; 0 turns it
  off). The cache key is the SQL and its parameters, never the question. Search results
  (2,000) and word similarities (200,000 pairs) are cached in memory too: on the PROD index,
  10 new searches took 3.2 s before and 2.3 s after on a laptop, a repeated search about 1 ms.
  Each container has its own caches; they start empty after a deploy or a cold start.
- **Typical latency** on `vigil_md_dev`: a device page 1.6–2.8 s, a counts or recall answer
  1.3–1.8 s, a narratives answer about 4 s.

```bash
VIGIL_BQ_DATASET=vigil_md_dev uvicorn api.app:app --port 8000
curl 'localhost:8000/api/search?q=dreamstation'
open http://localhost:8000/api/docs
```

### Monitoring events ([`api/events.py`](api/events.py))

Every `POST /api/answer` that reaches the chatbot writes one JSON line on stdout
(`"message": "router_decision"`). Cloud Run sends it to Cloud Logging, which stores the fields
as `jsonPayload`, so no deploy setting changes. Requests rejected before the chatbot (422, 429)
write none. These events are the input for model monitoring: label mix, `out_of_scope` share,
top score and unknown-word share compared with the router's test-set baseline.

| Field | Meaning |
|---|---|
| `request_id`, `ts`, `schema_version` | one id per answer, UTC time, event format (1) |
| `model_version`, `rules_fingerprint`, `template_fingerprint`, `dataset` | which router, rules and templates answered |
| `status`, `reply_kind`, `labels`, `rule_hits` | the decision; `rule_hits` are pattern names; `status` is `exception` if the chatbot raised |
| `scores`, `top_score`, `unclear` | router score per label |
| `unknown_word_share` | share of the question's words not in the router's word vocabulary (drift signal) |
| `question_chars`, `question_words` | size of the question |
| `entity_given`, `entity_types_found`, `n_entities` | device page or not; entity *types* found |
| `context_used` | short-term memory rule used: null, `previous_question` or `entity` (never the previous question's text) |
| `budget_gb_spent`, `latency_ms` | BigQuery GB used, time in the chatbot |

- **Never in an event:** the question text, the previous question sent as `context`, the search
  phrase, entity ids, the answer text, the visitor's address or headers. Tests check this with a question that contains a name.
- **Monitoring cannot break an answer:** an error while writing the event is logged and the
  answer is returned as usual.
- **In BigQuery:** the log sink `vigil-md-monitoring-events` (created 2026-11-19) copies the
  `router_decision` and `startup` events of the `vigil-md` service into the dataset
  `vigil_md_monitoring` (partitioned by day, rows deleted after 400 days). The sink's account can
  write to this dataset only. Undo: `gcloud logging sinks delete vigil-md-monitoring-events` and
  `bq rm -r -d vigil-md-1792696816:vigil_md_monitoring`.
- **Still to do:** the daily drift job. Storing question text (with notice to users, redaction and
  30-day expiry) is a separate, later step that needs a privacy review.
- **Search terms** (`GET /api/search?q=`) are in the URL, so Cloud Run's request log has them.
  They are device and company names, but keep this in mind for the privacy review.

### Drift check ([`monitoring/`](monitoring/))

Compares a window of monitoring events with a baseline of the same router. No BigQuery, no cost.

```bash
python -m monitoring.baseline --model-dir models/published   # baseline from the test set (bootstrap)
python -m monitoring.baseline --from-events reference.jsonl  # baseline from normal traffic (production)
python -m monitoring.drift --events events.jsonl --out report.json
```

- **Checks** ([`stats.py`](monitoring/stats.py)): PSI (Population Stability Index) of `top_score`,
  `unknown_word_share` and `question_words`; absolute change of each label's share, the refusal
  share and the unclear share. Limits in [`thresholds.json`](monitoring/thresholds.json): PSI 0.1
  warns, 0.25 is drift; a label share moving 0.15 (refusals or unclear 0.10) is drift.
- **Result and exit code:** `ok` or `warn` → 0; `insufficient_data` → 2 (fewer than 200 events,
  or none from the baseline's router); `drift` → 3, retraining should be considered.
- **Only the baseline's router is compared** (same rules fingerprint): after a new router is
  published, rebuild the baseline.
- **Two baselines:** [`baseline.json`](monitoring/baseline.json) is built from the hand-written
  test set (242 questions, router `5d1584bb53b3`). That set is about 40% medical-advice questions
  by design, so real traffic will differ on the refusal share. Replace it with a reference window
  of real traffic (for example the first 2–4 weeks after launch) once there is traffic; the
  thresholds were set before any live traffic and should be reviewed then too.
- **Checked with the real router:** the test-set questions as events give `ok`; 240 off-topic
  and non-English questions give `drift` on 8 checks (e.g. unknown-word PSI 5.3).
- **From BigQuery** ([`bq_events.py`](monitoring/bq_events.py)): `--from-bigquery --days 7`
  reads the window from `vigil_md_monitoring.run_googleapis_com_stdout` (dry run first, capped
  at `--max-gb`, default 0.1 GB; a window is a few MB). Before the first event, the table does
  not exist: the result is `insufficient_data` (exit 2) with a note. `--summary` writes a short
  Markdown report.
- **Daily job** ([`.github/workflows/router-monitoring.yml`](../../.github/workflows/router-monitoring.yml)):
  07:30 UTC and by hand (Actions → router-monitoring → Run workflow). Drift fails the run, so
  GitHub e-mails the alert; no decision is a warning; the report is kept for 90 days. It uses its
  own service account `vigil-md-monitor@vigil-md-1792696816.iam.gserviceaccount.com` (created
  2026-11-19: `roles/bigquery.jobUser` on the project, READER on `vigil_md_monitoring` only,
  keyless login for this repository's Actions), named in the repository variable
  `GCP_MONITOR_SA`; until that variable is set it says so and passes. Scheduled workflows run
  from `main` only. Undo: `gcloud iam service-accounts delete <account>` and remove its
  `roles/bigquery.jobUser` binding.
- **Retraining trigger:** on drift the daily job opens one GitHub issue "Router drift detected"
  (label `router-drift`) with the report and a checklist, or comments on the one already open.
  It does not retrain by itself: the router is trained on generated questions, so a retrain
  without new examples gives the same model. The checklist: new templates or slot values for
  the drifted kinds of questions and a fresh hand-written test set, then a pull request (CI
  retrains and gates), then the merge (publish and deploy), then a new baseline. The workflow
  uses GitHub's own token for this run, limited to `issues: write`.

## Container ([`Dockerfile`](Dockerfile))

One image holds the API, the website and the router. Build from the repository root; its
`.dockerignore` admits only the files the image copies.

```bash
docker build -f Model-Development/chatbot/Dockerfile --build-arg DATASET=vigil_md_dev -t vigil-md-app:dev .
docker run --rm -p 8090:8080 -v "$HOME/.config/gcloud:/home/app/.config/gcloud:ro" vigil-md-app:dev
curl localhost:8090/api/health
```

| Build argument | Values |
|---|---|
| `DATASET` | the search index copied in: `vigil_md_dev` or `vigil_md_prod` (build it first) |
| `MODEL` | `train` (default): retrain the router from the commit with the pinned versions, as CI does. `published`: copy the approved model from `models/published/`, downloaded with `publish.py --download`; the build fails if any file's SHA-256 differs from its manifest |

- The website is built in a `node:22` stage (`npm ci`, `npm run build` with
  `VITE_USE_MOCKS=false`).
- The server stage installs `requirements-serve.txt` only, runs as a non-root user and holds no
  credentials: mount gcloud credentials locally; on Cloud Run the service account is used.
- About 675 MB with the dev index; the PROD index adds 712 MB.

## Deployment (Cloud Run)

The website and API run as one private Cloud Run service; only signed-in project members can call
it.

| What | Value |
|---|---|
| Project, region | `vigil-md-1792696816`, `us-central1` |
| Image repository | Artifact Registry `vigil-md` (Docker), images tagged by commit |
| Service | `vigil-md`, `https://vigil-md-292904548224.us-central1.run.app`; 0–2 instances, 2 GiB, 1 CPU, 20 requests per instance, 60 s timeout |
| Service account | `vigil-md-chatbot@vigil-md-1792696816.iam.gserviceaccount.com`: `roles/bigquery.jobUser` and READER on `vigil_md_prod` only |
| Router | the published version, e.g. `5d1584bb53b3-eeb32c6` |

**Build and push** (from the repository root; Cloud Run is `linux/amd64`, so on an ARM Mac the
build is emulated, 10–20 minutes):

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev --quiet      # once per machine
cd Model-Development/chatbot
python -m query_router.publish --list                                # pick the version
python -m query_router.publish --download <version> --out models/published
python -m device_search.build_index --dataset vigil_md_prod          # current names, 0.15 GB
cd ../..
TAG=$(git rev-parse --short HEAD)
IMG=us-central1-docker.pkg.dev/vigil-md-1792696816/vigil-md/app:$TAG
docker buildx build --platform linux/amd64 -f Model-Development/chatbot/Dockerfile \
  --build-arg DATASET=vigil_md_prod --build-arg MODEL=published -t $IMG --push .
```

Wait until the push ends with `pushing manifest … done` before deploying.

**Deploy** (a new revision; earlier revisions stay available):

```bash
gcloud run deploy vigil-md --image $IMG --region us-central1 --project vigil-md-1792696816 \
  --service-account vigil-md-chatbot@vigil-md-1792696816.iam.gserviceaccount.com \
  --no-allow-unauthenticated --min-instances 0 --max-instances 2 \
  --memory 2Gi --cpu 1 --cpu-boost --concurrency 20 --timeout 60 \
  --set-env-vars VIGIL_BQ_DATASET=vigil_md_prod
```

**Check it:**

```bash
U=https://vigil-md-292904548224.us-central1.run.app
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" $U/api/health   # model_version
gcloud run services proxy vigil-md --region us-central1 --project vigil-md-1792696816 --port 8091
# open http://localhost:8091 (the private service through your login)
```

On a running instance a question takes about 2–9 s. The first request after the service scales to
zero takes 25–40 s while the image and the 712 MB index load.

**Roll back:** move traffic to an earlier revision,

```bash
gcloud run services update-traffic vigil-md --to-revisions <revision>=100 \
  --region us-central1 --project vigil-md-1792696816
```

or deploy an image built from an older published version.

**Automatic deploy (CI).** The `deploy` job of `.github/workflows/chatbot-publish.yml` does the
steps above after every publish on a push to `main` (chatbot or website changes):
it downloads the router version just published, restores or rebuilds the `vigil_md_prod` index
(rebuilt at most once a week or when `device_search/` changes, about 0.2 GB), builds and pushes
`app:<commit>`, and deploys it as a new revision **with no traffic** under the tag `ci-<commit>`.
`api/deploy_check.py` then calls that revision: `/api/health` must report the published router
version, `vigil_md_prod` and a built index, and a search must find something. Only then does all
traffic move to the new revision. If anything fails, the previous revision keeps serving. The
service's access is not changed (it stays private). The job runs once the repository variable
`GCP_DEPLOY_SA` is set; one-time setup (Owner on the project):

```bash
P=vigil-md-1792696816; SA=github-ci-deployer@$P.iam.gserviceaccount.com
gcloud iam service-accounts create github-ci-deployer --project $P --display-name "GitHub Actions chatbot deploy"
gcloud artifacts repositories add-iam-policy-binding vigil-md --project $P --location us-central1 \
  --member serviceAccount:$SA --role roles/artifactregistry.writer          # push the image
gcloud artifacts repositories add-iam-policy-binding vigil-md-models --project $P --location us-central1 \
  --member serviceAccount:$SA --role roles/artifactregistry.reader          # download the router
for R in run.developer run.invoker; do                                      # deploy, then check it
  gcloud run services add-iam-policy-binding vigil-md --region us-central1 --project $P \
    --member serviceAccount:$SA --role roles/$R
done
gcloud iam service-accounts add-iam-policy-binding vigil-md-chatbot@$P.iam.gserviceaccount.com --project $P \
  --member serviceAccount:$SA --role roles/iam.serviceAccountUser           # run revisions as the service's account
gcloud projects add-iam-policy-binding $P --member serviceAccount:$SA --role roles/bigquery.jobUser --condition=None
bq query --use_legacy_sql=false "GRANT \`roles/bigquery.dataViewer\` ON SCHEMA \`$P.vigil_md_prod\` TO \"serviceAccount:$SA\""
gcloud iam service-accounts add-iam-policy-binding $SA --project $P --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/292904548224/locations/global/workloadIdentityPools/github/attribute.repository/NainicaD/Vigil-MD"
```

Then add the repository variable `GCP_DEPLOY_SA` = `github-ci-deployer@vigil-md-1792696816.iam.gserviceaccount.com`
(it uses the same `GCP_WIF_PROVIDER` as the publish step).

**Make it public:** with the BigQuery daily query quota and budget alerts in place, grant
`gcloud run services add-iam-policy-binding vigil-md --region us-central1 --project
vigil-md-1792696816 --member=allUsers --role=roles/run.invoker` (undo with
`remove-iam-policy-binding`). The per-question cap, the rate limit and the 2-instance maximum
bound the cost of each visitor; the daily quota bounds the total.

**Cost:** the service scales to zero, so idle time costs nothing; `--min-instances 1` removes the
cold start for about $10–20 a month. Images cost about $0.10 a month each. BigQuery reads per
question are the main cost.

**Remove the deployment:**

```bash
gcloud run services delete vigil-md --region us-central1 --project vigil-md-1792696816
gcloud artifacts repositories delete vigil-md --location us-central1 --project vigil-md-1792696816
gcloud projects remove-iam-policy-binding vigil-md-1792696816 \
  --member=serviceAccount:vigil-md-chatbot@vigil-md-1792696816.iam.gserviceaccount.com --role=roles/bigquery.jobUser
# remove the account's READER entry on vigil_md_prod (console: dataset > Sharing > Permissions), then:
gcloud iam service-accounts delete vigil-md-chatbot@vigil-md-1792696816.iam.gserviceaccount.com --project vigil-md-1792696816
```

The container runs anywhere Docker does; give it credentials for the same read-only account as a
secret, never in git or the image.

## Development dataset (`vigil_md_dev`)

A small copy of `vigil_md_prod` with the same tables and layout, for development and the
integration tests. Built by [`scripts/build_dev_extract.py`](scripts/build_dev_extract.py) from
[`data/dev_extract.json`](data/dev_extract.json): product codes LZG (insulin pump), BZD (CPAP),
OZD (heart pump), FTR (breast implant) and PKU (app), MAUDE reports from 2023-01-01; about 550 MB.

| Table | Rows |
|---|---|
| `maude` | 459,482 |
| `maude_problems` | 1,148,678 |
| `maude_text` | 515,147 |
| `recall` / `enforcement` | 273 / 139 |
| `gudid` | 8,155 |
| `entity_reports` / `entity_catalog` | 1,041,954 / 1,063 |

```bash
python scripts/build_dev_extract.py --estimate-only    # dry runs (free)
python scripts/build_dev_extract.py --yes              # rebuild the extract (about 12.8 GB read once)
python ../../Data-Pipeline/scripts/warehouse/build_summaries.py --project vigil-md-1792696816 \
       --dataset vigil_md_dev --max-gb 1               # its summary tables (about 0.24 GB)
python -m device_search.build_index                    # its search index
```

The extract script writes only to a `vigil_md_dev*` dataset. To change what is copied, edit
`dev_extract.json`, bump `version`, and rebuild the extract, its summaries and its index.

## Tests

```bash
pytest -q                     # unit tests: fake router, index and BigQuery; no network, no cost
pytest -m integration -q      # on vigil_md_dev: query layer, search and answers vs independent SQL
```

Unit tests log to a temporary MLflow store. CI runs the unit tests, retrains the router and runs
the gate on every pull request; integration tests run on request only.
