"""Evaluate a trained classifier on the hand-written test set (data/test/).

Reports two views:
  model       the classifier's labels alone
  guarded     after the rule backstop (guardrails.route): what the chatbot actually uses
plus:
  advice_recall       share of medical-advice questions refused (the safety number; needs
                      "oos_kind": "advice" on those test rows)
  unrelated           unrelated-topic questions: refused, or left unclear (no label: the bot
                      asks to rephrase), or answered as a device question (missed)
  rule_false_alarms   in-scope test questions the rule backstop refuses (cost of strictness)
  slices              macro-F1 and out_of_scope recall per audience
  confusions          for each true label, which wrong labels were predicted with it

Usage (from Model-Pipeline/):
    python -m question_classifier.evaluate --test data/test/handwritten_test.jsonl
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .generate import read_jsonl
from .guardrails import rule_hits
from .labels import LABELS, OUT_OF_SCOPE, to_vector
from .metrics import score
from .predict import QuestionClassifier

OOS_IDX = LABELS.index(OUT_OF_SCOPE)


def evaluate(clf: QuestionClassifier, rows: list[dict]) -> dict:
    texts = [r["text"] for r in rows]
    y = np.array([to_vector(r["labels"]) for r in rows])
    s = clf.scores(texts)
    t = np.array([clf.thresholds[lbl] for lbl in LABELS])
    model_pred = (np.array([[row[lbl] for lbl in LABELS] for row in s]) >= t).astype(int)
    guarded = model_pred.copy()
    hits = [rule_hits(q) for q in texts]
    for i, h in enumerate(hits):
        if h:
            guarded[i, OOS_IDX] = 1

    in_scope = [i for i in range(len(rows)) if y[i, OOS_IDX] == 0]
    false_alarms = [{"text": texts[i], "rules": hits[i]} for i in in_scope if hits[i]]
    missed_oos = [texts[i] for i in range(len(rows)) if y[i, OOS_IDX] == 1 and guarded[i, OOS_IDX] == 0]

    advice = [i for i, r in enumerate(rows) if r.get("oos_kind") == "advice"]
    unrelated = [i for i, r in enumerate(rows) if r.get("oos_kind") == "unrelated"]
    advice_recall = (round(float(guarded[advice, OOS_IDX].mean()), 4) if advice else None)
    unrel = Counter("refused" if guarded[i, OOS_IDX] else
                    "unclear" if not guarded[i].any() else "answered" for i in unrelated)
    missed_advice = [texts[i] for i in advice if guarded[i, OOS_IDX] == 0]

    slices = {}
    by_aud = defaultdict(list)
    for i, r in enumerate(rows):
        by_aud[r.get("audience", "unknown")].append(i)
    for aud, idx in sorted(by_aud.items()):
        m = score(y[idx], guarded[idx])
        slices[aud] = {"n": len(idx), "macro_f1": m["macro_f1"],
                       "out_of_scope_recall": m["out_of_scope_recall"]}

    confusions = {}
    for j, label in enumerate(LABELS):
        c = Counter()
        for i in np.where(y[:, j] == 1)[0]:
            for k, other in enumerate(LABELS):
                if guarded[i, k] == 1 and y[i, k] == 0:
                    c[other] += 1
        confusions[label] = dict(c.most_common())

    return {"model": score(y, model_pred), "guarded": score(y, guarded),
            "advice_recall": advice_recall, "missed_advice": missed_advice,
            "unrelated": dict(unrel),
            "rule_false_alarms": {"n": len(false_alarms), "of_in_scope": len(in_scope),
                                  "examples": false_alarms[:20]},
            "missed_out_of_scope": missed_oos, "slices": slices, "confusions": confusions}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate on the hand-written test set.")
    ap.add_argument("--model-dir", type=Path, default=Path("models/baseline"))
    ap.add_argument("--test", type=Path, default=Path("data/test/handwritten_test.jsonl"))
    ap.add_argument("--out", type=Path, default=None, help="write the report as JSON")
    a = ap.parse_args(argv)
    report = evaluate(QuestionClassifier.load(a.model_dir), read_jsonl(a.test))
    g = report["guarded"]
    print(f"guarded: macro-F1 {g['macro_f1']}, out_of_scope recall {g['out_of_scope_recall']}, "
          f"false refusals {g['false_refusal_rate']}, exact match {g['exact_match']} (n={g['n']})")
    print(f"advice recall {report['advice_recall']} (medical-advice questions refused); "
          f"unrelated topics {report['unrelated']}")
    if report["missed_advice"]:
        print("MISSED medical-advice questions:", *report["missed_advice"], sep="\n  ")
    print(f"model only: macro-F1 {report['model']['macro_f1']}, "
          f"out_of_scope recall {report['model']['out_of_scope_recall']}")
    fa = report["rule_false_alarms"]
    print(f"rule false alarms: {fa['n']} of {fa['of_in_scope']} in-scope questions")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
