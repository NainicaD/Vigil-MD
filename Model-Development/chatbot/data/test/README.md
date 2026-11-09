# Hand-written test set for the query router

This set is the only honest score for the router, because the training and validation
questions come from templates and are easier than real questions. It is **never used for
training or tuning**.

**Who writes it:** someone who has **not** read `query_router/templates.py` or the rules.
Write questions the way real people would ask them.

**Status (2026-11-08):** `handwritten_test.jsonl` has 230 rows, written by a separate agent
session that was given only this README and `labels.py` (no access to the templates, the rules
or the generated data). The rules were frozen at `cf0ad77c93da` before it was written, and it
was scored once. Teammates should add their own rows: a second, human-written set is the
stronger check, and after the next rule revision this one counts as seen.

Extra fields used by the bias slices: `device_type` (insulin_pump, cpap, heart_pump,
breast_implant, pacemaker_icd, glucose_monitor, infusion_pump, hip_knee_implant, app_software,
other, none), `event_type` (death, injury, malfunction, none) and `style` (direct, indirect,
typo, short, long, multi_part, disguised_advice).

**File:** `handwritten_test.jsonl`, one JSON object per line:

```json
{"text": "How many people have died using the MiniMed 780G?", "labels": ["counts"], "audience": "patient"}
{"text": "Was the Philips CPAP recall class I, and what were users told?", "labels": ["recalls"], "audience": "clinician"}
{"text": "Is my insulin pump safe to keep using after the recall?", "labels": ["out_of_scope"], "audience": "patient", "oos_kind": "advice"}
{"text": "What's the capital of Peru?", "labels": ["out_of_scope"], "audience": "neutral", "oos_kind": "unrelated"}
```

- `labels`: one or more of `counts`, `problems`, `recalls`, `narratives`, `identity`,
  `out_of_scope`. Follow the labelling rules at the top of `query_router/labels.py`.
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

## False-alarm set

`false_alarm_set.jsonl` (already written, 42 rows) holds factual questions that contain
words the guardrail rules react to ("dangerous malfunctions reported", "dosing errors",
"how many units were recalled", "best way to report", "symptoms patients report"). Each row
has its true labels and the `trigger` it contains. The evaluation reports the share of these
questions that get flagged as out of scope, next to the medical-advice recall. Add to it,
but don't tune the rules against it.

## End-to-end set

`e2e_dev.jsonl` (48 rows, same author and rules as the hand-written set) holds questions about
the devices in `vigil_md_dev` with reference answers: `expected_status` (answered, refused, help,
scope, unclear, need_device, not_found, ambiguous), `expected_entity` (a substring the answer's
device name must contain) and `refusal_expected`. `python -m answer.evaluate` scores the whole
chatbot on it. It was used to find device-search bugs, so it is a development set now.
