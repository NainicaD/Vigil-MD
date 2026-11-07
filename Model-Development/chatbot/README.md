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

[`query_layer/`](query_layer/) fetches the facts for an answer with fixed, parameterized
BigQuery SQL. The router decides what to fetch; the device lookup (to build) decides which device.

```python
from query_layer import BigQueryBackend, Budget, device_summary, narratives, identity
backend = BigQueryBackend()                 # dataset: VIGIL_BQ_DATASET (default vigil_md_prod)
budget = Budget()                           # one per user question
device_summary(backend, "product_code", "LZG", budget)     # always: counts, top problems, recalls
narratives(backend, "brand", "MiniMed 780G", budget=budget) # when asked: a few report excerpts
identity(backend, "udi_di", "00643169007222", budget)      # when asked: who makes it (GUDID)
```

| Function | Returns | Tables |
|---|---|---|
| `device_summary` | Reports by event type and year (each report once); top 10 device and patient problem terms, without "no information" terms, plus the count of reports saying no patient harm; recalls newest first with FDA class, reason, root cause, action | `maude`, `maude_problems`, `recall`, `enforcement` |
| `narratives` | Up to 5 excerpts (about 600 characters, cut at a word), one per event type in turn (Death, Injury, Malfunction), newest first; texts under 50 characters skipped | `maude`, then `maude_text` for those reports only |
| `identity` | Manufacturer, device name, model, product codes | `gudid` |

- **Device keys:** `product_code`, `brand`, `manufacturer` (normalized name), `udi_di`. Recalls have
  no UDI: a `udi_di` key returns no recalls with a note (look it up by brand or product code).
- **Safety:** the key is always a bound parameter, never part of the SQL text; every statement
  is checked to be a single read-only `SELECT` before it is sent.
- **Cost guard:** before a step runs, its queries are dry-run (free). If together they would
  scan more than the question has left (`MAX_GB_PER_QUESTION`, default 2 GB), nothing runs
  (`QueryBudgetExceeded`); every job also has `maximum_bytes_billed`.
- **Sources:** every result carries the queries run (SQL, parameters, bytes) and the tables'
  last-modified times, for citations and for the gate.
- **Settings** in [`config.py`](query_layer/config.py) (environment variables override them);
  the non-informative and no-harm term lists in [`terms.py`](query_layer/terms.py).

**Measured cost on `vigil_md_prod`** (dry runs, 2026-11-07, `python -m query_layer.estimate`):

| Key | Device summary | Narratives |
|---|---|---|
| `product_code=LZG` | 3.55 GB | 0.73 GB |
| `brand=DreamStation` | 4.74 GB | 1.33 GB |
| `manufacturer=medtronic` | 4.22 GB | 1.06 GB |
| `udi_di` (no recalls) | 3.68 GB | 0.84 GB |

The cost hardly depends on the device: BigQuery reads whole columns, and `problems` (joining
`maude` with `maude_problems`) is about 3 GB of it. At this rate the free 1 TiB covers about
250 questions a month, and the 2 GB cap refuses the summary on PROD. **Daily summary tables**
(per device, built once a day in the data pipeline) are needed before real use; until then,
development runs against the small `vigil_md_dev` extract. The narrative texts themselves are
cheap (under 1 MB): `maude_text` is pruned by month and report key.
