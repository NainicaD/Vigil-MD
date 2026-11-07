# Vigil MD: Chatbot models

## Question classifier

The **"Classify question"** step of the chatbot design: it reads the user's question and
decides which fixed queries to run, and whether part of it must be refused.

```python
from question_classifier.predict import QuestionClassifier
clf = QuestionClassifier.load("models/baseline")
clf.classify("How many deaths has the t:slim X2 had? Should I stop using it?")
# {"labels": ["counts", "out_of_scope"], "run_queries": ["counts"],
#  "out_of_scope": True, "reply_kind": "medical_refusal",
#  "reply": "I can't give medical advice ...", "refusal": "I can't give medical advice ...",
#  "unclear": False, "rule_hits": ["should_i", "stop_switch"], "scores": {...}}
```

**Labels** (multi-label, rules in [`labels.py`](question_classifier/labels.py)): `counts`,
`problems`, `recalls`, `narratives`, `identity`, `out_of_scope`. It doesn't extract which
device or company the question names; that is a separate lookup step.

### Medical-advice guardrails

The bot never gives medical advice or judges whether a device is safe. Layers:

1. **Rule backstop** ([`guardrails.py`](question_classifier/guardrails.py)): patterns that
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

Every reply below is fixed text in [`guardrails.py`](question_classifier/guardrails.py), never
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

Generated from templates ([`templates.py`](question_classifier/templates.py)), including
55 medical-advice phrasings (38 indirect, such as "my dad's pump has hundreds of death
reports, is that bad?", "my doctor wants to put in X, thoughts?"), with real-world slot
values ([`slots.json`](question_classifier/slots.json)). Three disjoint splits, by device /
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
python -m question_classifier.generate          # data/generated/{train,val,heldout}.jsonl
python -m question_classifier.train             # models/baseline/{model.joblib,meta.json}
python -m question_classifier.evaluate          # heldout + false-alarm set
python -m question_classifier.evaluate --test data/test/handwritten_test.jsonl --out reports/test_report.json
pytest -q
```

If MLflow is installed (`pip install mlflow`), every training run is logged to the
`question-classifier` experiment.

### Next steps

- Teammates write the hand-written test set, with 20+ disguised advice questions
  ([`data/test/README.md`](data/test/README.md)).
- Small transformer (e.g. DistilBERT) fine-tuned for multi-label, compared with this baseline
  on the same frozen splits: goals are fewer false flags and recognising unseen phrasing.
- Optuna tuning, DVC stages (generate → train → evaluate), MLflow model registry.
- Replace `slots.json` values with names from the warehouse tables.
