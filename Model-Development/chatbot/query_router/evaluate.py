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
  safety              advice recall and false-refusal rate for rules alone, model alone and
                      both, so it is clear what each layer contributes
  in_scope_answered   in-scope questions still answered under each policy
evaluate_false_alarms() scores factual questions that contain trigger words
(data/test/false_alarm_set.jsonl). Every report records guardrails.rules_fingerprint().
  slices              macro-F1 and out_of_scope recall per audience
  confusions          for each true label, which wrong labels were predicted with it

MLflow: the metrics are added to the model's training run (mlflow_run_id in meta.json),
prefixed by --prefix ("heldout" by default, "test" for the hand-written set), with the full
report as an artifact. --no-log skips this.

Usage (from Model-Development/chatbot/):
    python -m query_router.evaluate                 # heldout + false-alarm set
    python -m query_router.evaluate --test data/test/handwritten_test.jsonl
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from .generate import read_jsonl
from .guardrails import rule_hits, rules_fingerprint
from .labels import LABELS, OUT_OF_SCOPE, to_vector
from .metrics import score
from .predict import QueryRouter

OOS_IDX = LABELS.index(OUT_OF_SCOPE)
FACTUAL_IDX = [i for i, lbl in enumerate(LABELS) if lbl != OUT_OF_SCOPE]
VIEWS = ("rules", "model", "both")


def _rate(mask: np.ndarray, idx) -> float | None:
    idx = list(idx)
    return round(float(np.asarray(mask)[idx].mean()), 4) if idx else None


def evaluate(clf: QueryRouter, rows: list[dict]) -> dict:
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
    rules = np.array([bool(h) for h in hits])
    flags = {"rules": rules, "model": model_pred[:, OOS_IDX] == 1,
             "both": guarded[:, OOS_IDX] == 1}
    safety = {v: {"advice_recall": _rate(flags[v], advice),
                  "false_refusal_rate": _rate(flags[v], in_scope)} for v in VIEWS}
    answered = guarded[:, FACTUAL_IDX].any(axis=1)
    in_scope_answered = {"facts_with_refusal": _rate(answered, in_scope),
                         "strict": _rate(answered & ~flags["both"], in_scope)}

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

    return {"rules_fingerprint": rules_fingerprint(), "n": len(rows), "n_advice": len(advice),
            "n_in_scope": len(in_scope), "safety": safety, "in_scope_answered": in_scope_answered,
            "model": score(y, model_pred), "guarded": score(y, guarded),
            "advice_recall": advice_recall, "missed_advice": missed_advice,
            "unrelated": dict(unrel),
            "rule_false_alarms": {"n": len(false_alarms), "of_in_scope": len(in_scope),
                                  "examples": false_alarms[:20]},
            "missed_out_of_scope": missed_oos, "slices": slices, "confusions": confusions}


def evaluate_false_alarms(clf: QueryRouter, rows: list[dict]) -> dict:
    """Factual questions with trigger words: share flagged by rules, model and both."""
    r = evaluate(clf, rows)
    return {"rules_fingerprint": r["rules_fingerprint"], "n": len(rows),
            "flagged_rate": {v: r["safety"][v]["false_refusal_rate"] for v in VIEWS},
            "still_answered_facts_with_refusal": r["in_scope_answered"]["facts_with_refusal"],
            "flagged_by_rules": r["rule_false_alarms"]["examples"]}


def report_metrics(report: dict, prefix: str) -> dict[str, float]:
    """The numbers to compare across models, as MLflow metric names. Pure function."""
    g, s = report["guarded"], report["safety"]
    out = {f"{prefix}_macro_f1": g["macro_f1"], f"{prefix}_exact_match": g["exact_match"],
           f"{prefix}_answered_facts_with_refusal":
               report["in_scope_answered"]["facts_with_refusal"]}
    for view in VIEWS:
        suffix = "" if view == "both" else f"_{view}"
        out[f"{prefix}_advice_recall{suffix}"] = s[view]["advice_recall"]
        out[f"{prefix}_false_refusal_rate{suffix}"] = s[view]["false_refusal_rate"]
    if "false_alarm_set" in report:
        for view, rate in report["false_alarm_set"]["flagged_rate"].items():
            out[f"falsealarm_flagged{'' if view == 'both' else '_' + view}"] = rate
    return {k: float(v) for k, v in out.items() if v is not None}


def log_to_training_run(model_dir: Path, report: dict, prefix: str) -> str | None:
    """Add the metrics and report to the model's training run. Returns the run id, or None
    when the model was trained without MLflow."""
    meta = json.loads((Path(model_dir) / "meta.json").read_text())
    run_id = meta.get("mlflow_run_id")
    if not run_id:
        print("meta.json has no mlflow_run_id: the metrics are not logged")
        return None
    from . import tracking
    mlflow = tracking.setup()
    with mlflow.start_run(run_id=run_id):
        mlflow.log_metrics(report_metrics(report, prefix))
        mlflow.set_tag(f"{prefix}_rules_fingerprint", report["rules_fingerprint"])
        mlflow.log_dict(report, f"eval/{prefix}_report.json")
    if report["rules_fingerprint"] != meta.get("rules_fingerprint"):
        print(f"WARNING: rules {report['rules_fingerprint']} differ from the training run's "
              f"{meta.get('rules_fingerprint')}: these numbers are for changed rules")
    return run_id


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate on the hand-written test set.")
    ap.add_argument("--model-dir", type=Path, default=Path("models/baseline"))
    ap.add_argument("--test", type=Path, default=Path("data/generated/heldout.jsonl"))
    ap.add_argument("--false-alarms", default="data/test/false_alarm_set.jsonl",
                    help="factual questions with trigger words ('' to skip)")
    ap.add_argument("--out", type=Path, default=None, help="write the report as JSON")
    ap.add_argument("--prefix", default="heldout",
                    help="MLflow metric prefix: heldout, or test for the hand-written set")
    ap.add_argument("--no-log", action="store_true", help="don't log to MLflow")
    a = ap.parse_args(argv)
    clf = QueryRouter.load(a.model_dir)
    report = evaluate(clf, read_jsonl(a.test))
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
    print(f"rules {report['rules_fingerprint']}   {'rules':>8}{'model':>8}{'both':>8}")
    for key in ("advice_recall", "false_refusal_rate"):
        print(f"{key:20}" + "".join(f"{report['safety'][v][key]!s:>8}" for v in VIEWS))
    print(f"in-scope still answered: {report['in_scope_answered']}")
    if a.false_alarms:
        far = evaluate_false_alarms(clf, read_jsonl(Path(a.false_alarms)))
        report["false_alarm_set"] = far
        print(f"false-alarm set flagged (n={far['n']}): {far['flagged_rate']}, still answered "
              f"under facts_with_refusal: {far['still_answered_facts_with_refusal']}")
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(report, indent=2))
    if not a.no_log:
        run_id = log_to_training_run(a.model_dir, report, a.prefix)
        if run_id:
            print(f"logged {a.prefix}_* metrics to MLflow run {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
