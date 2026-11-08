# Vigil MD: Chatbot models

## Query router

The **"Classify question"** step of the chatbot design, named the query router: it reads the
user's question and decides which fixed queries to run, whether part of it must be refused,
or which fixed reply to give. Inside it, a trained classifier and guardrail rules decide together.

```python
from query_router.predict import QueryRouter
router = QueryRouter.load("models/baseline")
router.classify("How many deaths has the t:slim X2 had? Should I stop using it?")
# {"labels": ["counts", "out_of_scope"], "run_queries": ["counts"],
#  "out_of_scope": True, "reply_kind": "medical_refusal",
#  "reply": "I can't give medical advice ...", "refusal": "I can't give medical advice ...",
#  "unclear": False, "rule_hits": ["should_i", "stop_switch"], "scores": {...}}
```

**Labels** (multi-label, rules in [`labels.py`](query_router/labels.py)): `counts`,
`problems`, `recalls`, `narratives`, `identity`, `out_of_scope`. It doesn't extract which
device or company the question names; that is a separate lookup step.

### Medical-advice guardrails

The bot never gives medical advice or judges whether a device is safe. Layers:

1. **Rule backstop** ([`guardrails.py`](query_router/guardrails.py)): patterns that
   fire only when the user asks the bot to **judge or advise**: "is/are X safe", "safe
   to/for", "should I…", first-person "should I stop / I want to switch", "which is best
   for me / on the market" (not "best way to report"), "better than", "how much insulin /
   how many units … should I take", first-person symptoms ("I have a rash", "my dad has
   swelling"), "would you recommend", "can I still use/wear/take". A trigger word alone
   ("dangerous malfunctions reported", "dosing errors", "units recalled") does not fire.
2. **Classifier threshold** for `out_of_scope`, tuned with the rules for ≥ 98% recall on
   the **medical-advice** validation questions (unrelated topics don't drive it), capped at 0.5.
3. **Routing**, default policy **`facts_with_refusal`**: the advice part gets a fixed
   refusal text (never model-written, points to a doctor and FDA MedWatch) and the factual
   part is still answered. A false alarm then costs a refusal note, not the answer.
   `strict` (refuse everything) is available.
4. The chatbot's **output gate** (team design) still blocks advice wording in drafts.

### Fixed replies

Every reply below is fixed text in [`guardrails.py`](query_router/guardrails.py), never
written by a model. `classify()` returns which one to show in `reply_kind` and its text in `reply`.

| `reply_kind` | When | Text starts |
|---|---|---|
| `help` | The **whole** question asks about the bot ("what can you do", "help", "who are you", "how do I use this tool"), and no advice rule fires. No query runs. | "I answer questions about FDA reports and recalls for medical devices…" |
| `medical_refusal` | `out_of_scope` with any health or device context: an advice rule, a factual label, a health or device word, or a word with a digit (a model number such as "780G") | "I can't give medical advice… If this is an emergency, call 911…" |
| `scope` | `out_of_scope` with none of those signs ("write me a poem") | "I can only answer questions about FDA reports and recalls…" |

When in doubt the reply is `medical_refusal`, so these replies never weaken safety. They change
only the text the user sees, not which questions are refused: the advice rules and their
fingerprint are unchanged. Known gaps: an unrelated question that trips an advice rule ("what
stocks should I buy") gets the medical refusal; advice naming a brand without digits and no health
word ("can I shower with my DreamStation") gets the scope reply, which still gives no advice. The
device lookup (next step) will recognise brand names.

**The rules are frozen.** `rules_fingerprint()` (currently `0dd7a4dd862a`) is stored with
every trained model and evaluation. The held-out numbers below are honest only for this
fingerprint: if a rule changes, the held-out split has been seen and a new one (or the
hand-written test set) is needed.

### Data

Generated from templates ([`templates.py`](query_router/templates.py)), including
55 medical-advice phrasings (38 indirect, such as "my dad's pump has hundreds of death
reports, is that bad?", "my doctor wants to put in X, thoughts?"), with real-world slot
values ([`slots.json`](query_router/slots.json)). Three disjoint splits, by device /
company / product-code names **and** by phrasing:

| Split | Share | Used for |
|---|---|---|
| train | 60% | fitting the model |
| val | 20% | choosing C and thresholds |
| **heldout** | 20% | **never used for tuning**; scored once, after the rules were frozen |

Plus two fixed sets in [`data/test/`](data/test/README.md): the **false-alarm set** (42
factual questions containing trigger words) and the **hand-written test set** teammates will
write (the real measure).

### Results (held-out split, rules `0dd7a4dd862a`, 2026-11-07)

Baseline: TF-IDF (words + characters) + one logistic regression per label, C = 1.

| Safety | rules alone | model alone | **both** |
|---|---|---|---|
| **Medical-advice questions flagged** (192) | 0.328 | 0.969 | **0.990** |
| Normal questions flagged by mistake (769) | 0.000 | 0.291 | 0.291 |
| **False-alarm set flagged** (42) | 0.071 | 0.333 | 0.381 |

- **Normal questions still answered:** 94.8% under `facts_with_refusal`, 70.4% under
  `strict`. The remaining 5% get no factual label at all (label misses, not refusals).
- **Missed advice** (2 of 192): "do you think [pump] is trustworthy" and "my doctor wants to
  put in a [device], thoughts", both inside a longer factual question.
- **False-alarm set, rules:** 3 of 42, the edge cases expected when the set was written
  ("was okay after inspection", "found the device safe to continue using", "how much insulin
  was over-delivered"). The model flags 14, 13 of them not caught by a rule (for example
  "Is there a recall on the Omnipod 5?"); with `facts_with_refusal` 95% are still answered.
- **Label quality on unseen phrasing is weak:** macro-F1 0.63, and `identity` 0.0 (its two
  held-out phrasings were never recognised).

**What this shows:** the narrowed rules now catch a third of advice questions on their own;
the model carries most of the safety, at the cost of flagging about 29% of normal questions.
On validation, ≥ 98% advice recall required a threshold that flags 19% of normal questions.
The default policy keeps most of those answered. Lowering false flags and fixing unseen
phrasing are the next model's job.

### Run it (from `Model-Development/chatbot/`, all on a laptop, no cost)

```bash
pip install -r requirements.txt
python -m query_router.generate          # data/generated/{train,val,heldout}.jsonl
python -m query_router.train             # models/baseline/{model.joblib,meta.json} + MLflow run
python -m query_router.evaluate          # heldout + false-alarm set, added to that run
python -m query_router.evaluate --test data/test/handwritten_test.jsonl --prefix test --out reports/test_report.json
pytest -q                                       # logs to a temporary MLflow store only
```

### Experiment tracking (MLflow)

Follows the shared conventions in [`../MLFLOW.md`](../MLFLOW.md); the code is in
[`tracking.py`](query_router/tracking.py).

- **Store:** `Model-Development/mlflow/` on your machine (not in git). View it from the repo root
  with `mlflow ui --backend-store-uri sqlite:///Model-Development/mlflow/mlflow.db --port 5050`,
  then open http://127.0.0.1:5050 (port 5000 is taken by AirPlay on macOS).
- **`train`:** one parent run `tfidf_lr-grid` in experiment `chatbot/query-router`, with
  one nested run per C. The parent run has the required tags (`git_commit`, `git_dirty`,
  `data_version`, `author`, `workstream`) plus `rules_fingerprint`, the chosen C, the `val_*`
  metrics (including `val_advice_recall`), the thresholds, the model files, and the model,
  registered as a new version of `chatbot-query-router`. `--alias champion` points
  the alias at it. `meta.json` gets `mlflow_run_id` and `model_version`.
- **`evaluate`:** adds `heldout_*` metrics (advice recall and false refusals for rules,
  model and both; macro-F1; share still answered) and `falsealarm_flagged*` to the same
  run, plus the full report as an artifact. `--prefix test` for the hand-written set;
  `--no-log` to skip.

### Next steps

- **One rule revision, before the hand-written test set is written** (any rule change makes the
  held-out numbers stale, so all changes go in one batch, then the rules are frozen again):
    - Add patterns for advice the rules miss today: opinion-seeking ("do you think X is
      trustworthy", "would you trust…", "…, thoughts?", "is it worth switching") and urgent
      device trouble ("my pump says error 47 and I'm low, help"), so these always get the
      refusal with the emergency line without relying on the model.
    - Optional: stop refusing factual questions that trip a rule today, from the false-alarm
      set: "how much insulin was over-delivered in the reports" (`dosing`), "found the device
      safe to continue using" (`safe_to`), "the pump was okay after inspection"
      (`safety_judgement`).
    - Check against the false-alarm set and the advice templates, freeze (new fingerprint),
      then score once on the hand-written test set. Log the new model as `challenger`; it
      replaces `champion` only if it does better there.
- Teammates write the hand-written test set, with 20+ disguised advice questions
  ([`data/test/README.md`](data/test/README.md)).
- Small transformer (e.g. DistilBERT) fine-tuned for multi-label, compared with this baseline
  on the same frozen splits: goals are fewer false flags and recognising unseen phrasing.
- Optuna tuning, DVC stages (generate → train → evaluate).
- Replace `slots.json` values with names from the warehouse tables.

## Query layer

[`query_layer/`](query_layer/) fetches the facts for an answer with fixed, parameterized SQL on
the **daily summary tables** (built by `Data-Pipeline/scripts/warehouse/build_summaries.py`;
see that README's "Daily summary tables"). A question costs MBs, not GBs.

```python
from query_layer import BigQueryBackend, Budget, device_summary, narratives, identity
backend = BigQueryBackend()                 # dataset: VIGIL_BQ_DATASET (default vigil_md_prod)
budget = Budget()                           # one per user question (cap 0.5 GB)
device_summary(backend, "device", "dev:f0cdc15b4d63", budget)   # always: entity, counts, problems, recalls
narratives(backend, "product_code", "OZD", budget=budget)       # when asked: a few report excerpts
identity(backend, "device", "dev:f0cdc15b4d63", budget)         # when asked: who makes it
```

An **entity** is `(entity_type, entity_id)`, the same in the summary tables and the search index:

| `entity_type` | `entity_id` | Its reports |
|---|---|---|
| `device` | `dev:` + 12 hex (GUDID records grouped by company and brand) | MAUDE rows with one of its UDIs (exact UDI links only) |
| `product_code` | the FDA product code, e.g. `LZG` | MAUDE rows with that product code |
| `manufacturer` | `manufacturer_name_norm`, e.g. `respironics` | MAUDE rows with that name |

| Function | Returns | Reads |
|---|---|---|
| `device_summary` | `entity` (title, company, product codes, `found`); `counts` (total, by event type, by year) and `counts_note` (for a device: "linked by UDI"); `problems` (top 10 device and patient terms, no-information terms already removed, `no_harm_reports`); `recalls` (total, newest 50 with class, reason, root cause, action, `matched_by`) | `entity_catalog`, `entity_counts`, `entity_problems`, `entity_recalls` |
| `narratives` | up to 5 excerpts (about 600 characters, cut at a word), one per event type in turn (Death, Injury, Malfunction), newest first; texts under 50 characters skipped | `entity_reports`, then `maude_text` for those reports only |
| `identity` | a device's GUDID name, company, model numbers, product codes; for a product code or manufacturer, its catalog entry (top brand names) | `device_catalog` or `entity_catalog` |

- **One partition per lookup:** every query filters on `bucket` (`config.entity_bucket`, the
  same function as the pipeline's), `entity_type` and `entity_id`. On `vigil_md_prod`
  (2026-11-07): a device summary reads 0.17 MB (before the summary tables: 3.5-4.7 GB);
  narratives for DreamStation 3.1 MB, for the largest product code (DZE) 220 MB. BigQuery
  bills at least 10 MB per table read, so a typical question bills about 40-50 MB.
- **Safety:** the entity is always a bound parameter; every statement is checked to be a
  single read-only `SELECT` before it is sent.
- **Cost guard:** before a step runs, its queries are dry-run (free); if together they would
  read more than the question has left (`MAX_GB_PER_QUESTION`, 0.5 GB), nothing runs
  (`QueryBudgetExceeded`); every job has `maximum_bytes_billed`.
- **Sources:** every result carries the queries run (SQL, parameters, bytes) and the tables'
  last-modified times, for citations and for the gate.
- **Settings:** [`config.py`](query_layer/config.py); `python -m query_layer.estimate
  --entity-type device --entity-id dev:f0cdc15b4d63` prints what each query would read (free).

### Development dataset (`vigil_md_dev`)

A small copy of `vigil_md_prod` with the same table names and layout, for development and the
integration tests. Built by [`scripts/build_dev_extract.py`](scripts/build_dev_extract.py) from
[`data/dev_extract.json`](data/dev_extract.json): product codes LZG (insulin pump), BZD (CPAP,
Class I recall), OZD (heart pump, death-heavy), FTR (breast implant, injury-heavy), PKU (app, no
recalls), MAUDE reports from 2023-01-01. Its summary tables are built with the pipeline's
script (`--dataset vigil_md_dev`).

| Table | Rows (v1, 2026-11-07) |
|---|---|
| `maude` | 459,482 |
| `maude_problems` | 1,148,678 |
| `maude_text` | 515,147 |
| `recall` / `enforcement` | 273 / 139 |
| `gudid` | 8,155 |
| summary tables | `entity_reports` 1,041,954; `entity_catalog` 1,063; others smaller |

About 550 MB in total. Building the extract read 12.8 GB from PROD once; its summary tables 0.24 GB.

```bash
# from Model-Development/chatbot/
python scripts/build_dev_extract.py --estimate-only    # dry runs (free)
python scripts/build_dev_extract.py --yes              # rebuild the extract (replaces each table)
python ../../Data-Pipeline/scripts/warehouse/build_summaries.py --project vigil-md-1792696816 \
       --dataset vigil_md_dev --max-gb 1               # rebuild its summary tables (about 0.24 GB)
python -m device_search.build_index                    # its search index (a few MB)
pytest -m integration -q                               # 12 tests on vigil_md_dev, about 0.3-0.5 GB
```

The extract script writes only to a `vigil_md_dev*` dataset. To change what is copied, edit
`dev_extract.json`, bump `version`, and rebuild the extract, its summaries and its index.
`pytest` alone skips the integration tests (marker `integration`), as does CI. They compare the
query layer's answers (from the summary tables) with independent SQL on the raw dev tables.

## Device search

[`device_search/`](device_search/) finds entities from what people type. It is **shared by the
website's search box and the chatbot**, so both find the same entity. Search runs on a SQLite
file (`data/search/<dataset>.sqlite`, not in git), built from the summary tables
`entity_catalog` and `device_catalog`; searching costs nothing.

```python
from device_search import DeviceIndex
index = DeviceIndex.open("vigil_md_dev")
index.search("dreamstaton")                                   # ranked devices, manufacturers, codes
r = index.resolve("How many deaths were reported for the dreamstaton?")
r.status       # resolved | several ("X vs Y") | ambiguous (bot asks which) | none (bot asks)
device_summary(backend, **index.query_key(r.matches[0]), budget=budget)
```

- **Entries:** every device, product code and manufacturer in `entity_catalog`, with the same
  `entity_type` and `entity_id` as the query layer (`query_key(hit)` gives them). Devices also
  carry their model numbers and UDIs (`udis_for(hit)`).
- **Matching:** words in names and model numbers, typos allowed (similarity, 4-letter prefixes,
  plurals); a device's company name counts slightly less, so "medtronic" ranks manufacturers
  first and "philips dreamstation" finds the device. Equal scores rank by report count.
- **Owner names:** [`aliases.json`](device_search/aliases.json) maps names users type to names
  in the data (Philips → Respironics). The matching team's company grouping can replace it.
- **In a question:** a UDI (8-14 digits) wins, then a product code in capitals, then the
  question's device words (question and judgement words removed). The top match is taken
  when it is alone, or has 3x the reports of an equal match from the same company (or another
  spelling of a manufacturer name), or 20x those of an equal match from another company.
  Otherwise the bot shows the top matches and asks: "breast implants" matches several
  companies' products (MENTOR 22,152 vs Natrelle 5,191 reports) and asks.
- **Report counts for devices are exact UDI links only.** Many reports have no UDI:
  DreamStation has 30,356 linked reports in `vigil_md_prod`, while product code BZD has many
  more. Answers should say "reports linked by UDI". The matching team's predicted links will add
  the rest, through the pipeline's summary tables, with no change here.

```bash
# from Model-Development/chatbot/
python -m device_search.build_index --estimate-only                     # vigil_md_dev: about 0 GB
python -m device_search.build_index                                     # data/search/vigil_md_dev.sqlite
python -m device_search.build_index --dataset vigil_md_prod --estimate-only   # about 0.15 GB
```

On `vigil_md_dev`: 838 devices, 220 manufacturers, 5 product codes, 8,155 UDIs; a search
takes 2 to 55 ms. Rebuild the index after the summary tables change (daily on PROD).

## Handoff: building the answer (step 7)

The pieces for one chatbot turn, in order. Everything returns plain dicts.

1. `QueryRouter.load("models/baseline").classify(question)`: `reply_kind` (`help`,
   `medical_refusal`, `scope`: show `reply`, the fixed text), `run_queries` (which parts were
   asked: `counts`, `problems`, `recalls`, `narratives`, `identity`), `unclear` (ask to
   rephrase).
2. `DeviceIndex.open(dataset).resolve(question)`: which entity. `ambiguous` → show
   `matches` as choices; `none` → ask which device; `several` → one summary per match. A
   device page can skip this and pass its entity directly. Keep the entity for follow-up
   questions in the same conversation.
3. With one `Budget()` per question: always `device_summary(...)`; `narratives(...)` if
   `narratives` is in `run_queries`; `identity(...)` if `identity` is.
4. Write the answer from the returned numbers only, and cite `sources`. Rules from the design
   doc: "N reports describe…", never a safety judgement; MAUDE counts reports, not failure
   rates; for a device say "reports linked by UDI"; `(B)(4)` / `(B)(6)` in narratives mean
   text withheld by the FDA; if the router set `out_of_scope` with facts (policy
   `facts_with_refusal`), start with the refusal text.

Develop against `vigil_md_dev` (`VIGIL_BQ_DATASET=vigil_md_dev`, index `vigil_md_dev`): a
question there bills a few tens of MB.
