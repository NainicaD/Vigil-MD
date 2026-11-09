"""The function the chatbot calls: question → labels, scores and a routing decision.

    clf = QueryRouter.load("models/baseline")
    clf.classify("How many deaths has the t:slim X2 had? Should I stop using it?")
    # {"labels": ["counts", "out_of_scope"], "scores": {...},
    #  "rule_hits": ["should_i", "stop_switch"], "out_of_scope": True,
    #  "run_queries": ["counts"], "refusal": "I can't give medical advice ...", "unclear": False}

With the default policy (facts_with_refusal) the answer starts with the refusal text and
then answers only the factual part. QueryRouter.load(dir, policy="strict") runs no
query at all for out_of_scope questions.

"unclear" is True when no label passes its threshold, no guardrail rule fires and the
question is not a help request: the chatbot should ask the user to rephrase instead of
guessing. "reply_kind" / "reply" give the fixed text to show (help, medical refusal or
scope reply; see guardrails.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from .guardrails import DEFAULT_POLICY, route, rules_fingerprint
from .labels import LABELS


class QueryRouter:
    def __init__(self, pipe, thresholds: dict[str, float], policy: str = DEFAULT_POLICY):
        self.pipe, self.thresholds, self.policy = pipe, thresholds, policy

    @classmethod
    def load(cls, model_dir: str | Path, policy: str = DEFAULT_POLICY) -> "QueryRouter":
        import joblib
        model_dir = Path(model_dir)
        meta = json.loads((model_dir / "meta.json").read_text())
        if meta["labels"] != list(LABELS):
            raise ValueError(f"model labels {meta['labels']} != {list(LABELS)}")
        # The thresholds were tuned together with the guardrail rules: a model trained under
        # other rules must be retrained, not used silently.
        trained, current = meta.get("rules_fingerprint"), rules_fingerprint()
        if trained != current:
            raise ValueError(f"model in {model_dir} was trained with guardrail rules {trained}, "
                             f"the code has {current}: run python -m query_router.generate and "
                             f"python -m query_router.train")
        return cls(joblib.load(model_dir / "model.joblib"), meta["thresholds"], policy)

    def scores(self, questions: list[str]) -> list[dict[str, float]]:
        probs = self.pipe.predict_proba(questions)
        return [{lbl: round(float(p), 4) for lbl, p in zip(LABELS, row)} for row in probs]

    def classify(self, question: str) -> dict:
        s = self.scores([question])[0]
        labels = [lbl for lbl in LABELS if s[lbl] >= self.thresholds[lbl]]
        decision = route(labels, question, self.policy)
        return {**decision, "scores": s,
                "unclear": not decision["labels"] and decision["reply_kind"] is None}
