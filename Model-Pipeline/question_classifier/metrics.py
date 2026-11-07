"""Multi-label metrics and per-label threshold tuning (pure functions on numpy arrays)."""
from __future__ import annotations

import numpy as np

from .labels import LABELS, OUT_OF_SCOPE

OOS_RECALL_TARGET = 0.98       # out_of_scope must catch at least this share on validation
OOS_MAX_THRESHOLD = 0.5        # never above this: synthetic validation is easier than real
                               # questions, so the tuned value would be too permissive
THRESHOLD_GRID = np.round(np.arange(0.05, 0.96, 0.01), 2)


def _prf(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[float, float, float]:
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def _fbeta(p: float, r: float, beta: float) -> float:
    b2 = beta * beta
    return (1 + b2) * p * r / (b2 * p + r) if p + r else 0.0


def tune_thresholds(probs: np.ndarray, y: np.ndarray, oos_recall: float = OOS_RECALL_TARGET,
                    rule_mask: np.ndarray | None = None) -> dict[str, float]:
    """Per label: the threshold with the best F1.

    out_of_scope is tuned for what the chatbot uses: model OR rule backstop (rule_mask,
    True where a guardrail rule fires). It takes the highest threshold whose combined
    recall meets oos_recall (fewest false refusals at the required recall), capped at
    OOS_MAX_THRESHOLD. If no threshold meets it, the one with the best F2 (recall weighted
    twice) - never simply the lowest, which would refuse many legitimate questions."""
    rules = np.zeros(len(y), dtype=bool) if rule_mask is None else np.asarray(rule_mask, bool)
    out = {}
    for j, label in enumerate(LABELS):
        if label == OUT_OF_SCOPE:
            stats = [(t, *_prf(y[:, j], ((probs[:, j] >= t) | rules).astype(int)))
                     for t in THRESHOLD_GRID]
            ok = [t for t, _, r, _ in stats if r >= oos_recall]
            if ok:
                out[label] = min(float(max(ok)), OOS_MAX_THRESHOLD)
            else:
                best = max(stats, key=lambda s: (_fbeta(s[1], s[2], 2.0), s[0]))
                out[label] = min(float(best[0]), OOS_MAX_THRESHOLD)
        else:
            scores = [_prf(y[:, j], (probs[:, j] >= t).astype(int))[2] for t in THRESHOLD_GRID]
            out[label] = float(THRESHOLD_GRID[int(np.argmax(scores))])
    return out


def apply_thresholds(probs: np.ndarray, thresholds: dict[str, float]) -> np.ndarray:
    t = np.array([thresholds[lbl] for lbl in LABELS])
    return (probs >= t).astype(int)


def score(y: np.ndarray, pred: np.ndarray) -> dict:
    per = {}
    for j, label in enumerate(LABELS):
        p, r, f = _prf(y[:, j], pred[:, j])
        per[label] = {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4),
                      "support": int(y[:, j].sum())}
    present = [lbl for lbl in LABELS if per[lbl]["support"] > 0]
    macro = float(np.mean([per[lbl]["f1"] for lbl in present])) if present else 0.0
    j = LABELS.index(OUT_OF_SCOPE)
    in_scope = y[:, j] == 0
    false_refusal = float(pred[in_scope, j].mean()) if in_scope.any() else 0.0
    return {"macro_f1": round(macro, 4),
            "exact_match": round(float((y == pred).all(axis=1).mean()), 4) if len(y) else 0.0,
            "out_of_scope_recall": per[OUT_OF_SCOPE]["recall"],
            # in-scope questions refused under the strict policy
            "false_refusal_rate": round(false_refusal, 4),
            "per_label": per, "n": int(len(y))}
