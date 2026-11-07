# Vigil MD: Model Pipeline

## Question classifier

The **"Classify question"** step of the chatbot design: it reads the user's question and
decides which fixed queries to run, or refuses.

```python
from question_classifier.predict import QuestionClassifier
clf = QuestionClassifier.load("models/baseline")
clf.classify("How many deaths has the t:slim X2 had, and was it recalled?")
# {"labels": ["counts", "recalls"], "run_queries": ["counts", "recalls"],
#  "out_of_scope": False, "refusal": None, "unclear": False, "scores": {...}, "rule_hits": []}
```

**Labels** (multi-label, rules in [`labels.py`](question_classifier/labels.py)): `counts`,
`problems`, `recalls`, `narratives`, `identity`, `out_of_scope`. It doesn't extract which
device or company the question names; that is a separate lookup step.

### Medical-advice guardrails

The bot never gives medical advice or judges whether a device is safe. That is enforced in
layers, so it doesn't depend on the model alone:

1. **Rule backstop** ([`guardrails.py`](question_classifier/guardrails.py)): fixed patterns
   ("should I…", "is it safe…", "stop using…", "which is best…", dosing, symptoms) mark a
   question `out_of_scope` whatever the model says.
2. **Classifier threshold** for `out_of_scope`, tuned together with the rules for at least
   98% recall on validation, and capped at 0.5.
3. **Routing:** `out_of_scope` overrides every other label. With the default `strict`
   policy no query runs and the user gets a fixed refusal text (never model-written) that
   points to their doctor and to FDA MedWatch. The `facts_with_refusal` policy instead
   refuses the advice part and still answers the factual part.
4. The chatbot's **output gate** (team design) still blocks advice wording in drafts.

### Data

- **Training and validation:** generated from templates
  ([`templates.py`](question_classifier/templates.py)) with real-world slot values
  ([`slots.json`](question_classifier/slots.json)). Validation uses device, company and
  product-code names **and** about a quarter of each label's phrasings that training never
  sees.
- **Test:** hand-written by teammates, never used for training or tuning. See
  [`data/test/README.md`](data/test/README.md).

### Run it (from `Model-Pipeline/`, all on a laptop, no cost)

```bash
pip install -r requirements.txt
python -m question_classifier.generate          # data/generated/{train,val}.jsonl
python -m question_classifier.train             # models/baseline/{model.joblib,meta.json}
python -m question_classifier.evaluate --test data/test/handwritten_test.jsonl --out reports/test_report.json
pytest -q
```

If MLflow is installed (`pip install mlflow`), every training run is logged to the
`question-classifier` experiment (`mlflow ui` to browse).

### Baseline results (validation, 2026-11-07)

TF-IDF (words + characters) + logistic regression, one classifier per label. Validation
uses held-out wording, so it's harder than training; the hand-written test set is still
the real measure.

| Metric (with guardrail rules applied) | Value |
|---|---|
| **Medical-advice questions refused** | **100%** (82 of 82) |
| In-scope questions refused by mistake | 6.5% |
| Rule false alarms on in-scope questions | 0 of 648 |
| Macro-F1 across labels | 0.87 |
| Macro-F1 by audience | researcher 0.96, neutral 0.96, clinician 0.92, procurement 0.89, **patient 0.62** |

The patient slice is the weak spot: plain wording such as "what happened to people who
were hurt by…" is harder for a word-based model. That is the main thing the next model
(a small transformer) should improve.

### Next steps

- Teammates write the hand-written test set (`data/test/README.md`).
- Small transformer model (e.g. DistilBERT) fine-tuned for multi-label, compared with the
  baseline; Optuna for tuning.
- DVC stages (generate → train → evaluate) and MLflow model registry.
- Replace `slots.json` values with device, company and product-code values from the
  warehouse tables.
