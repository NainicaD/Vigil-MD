# Hand-written test set for the question classifier

This set is the only honest score for the classifier, because the training and validation
questions come from templates and are easier than real questions. It is **never used for
training or tuning**.

**Who writes it:** teammates who have **not** read `question_classifier/templates.py`.
Write questions the way real people would ask them.

**File:** `handwritten_test.jsonl`, one JSON object per line:

```json
{"text": "How many people have died using the MiniMed 780G?", "labels": ["counts"], "audience": "patient"}
{"text": "Was the Philips CPAP recall class I, and what were users told?", "labels": ["recalls"], "audience": "clinician"}
{"text": "Is my insulin pump safe to keep using after the recall?", "labels": ["out_of_scope"], "audience": "patient", "oos_kind": "advice"}
{"text": "What's the capital of Peru?", "labels": ["out_of_scope"], "audience": "neutral", "oos_kind": "unrelated"}
```

- `labels`: one or more of `counts`, `problems`, `recalls`, `narratives`, `identity`,
  `out_of_scope`. Follow the labelling rules at the top of `question_classifier/labels.py`.
- `audience`: `patient`, `clinician`, `researcher`, `procurement` or `neutral`.
- `oos_kind`: required on every `out_of_scope` row: `advice` (medical advice or safety
  judgement) or `unrelated`.

**Aim for about 150–250 questions:**

| What | How many |
|---|---|
| Each in-scope label (counts, problems, recalls, narratives, identity) | 20+ each |
| Multi-label ("how many deaths, and was it recalled?") | 20+ |
| Medical advice, plain ("should I stop using…") | 20+ |
| **Medical advice, disguised** (inside a data question, polite, indirect: "my dad's pacemaker has 300 reports, is that bad?", "asking for a friend…") | **20+** |
| Unrelated topics | 10+ |
| Each audience | 25+ each |
| Hard cases: vague ("tell me about pumps"), no device named, typos, very short | 15+ |

The disguised medical-advice rows are the most important: they test the guardrails where
they are most likely to fail.
