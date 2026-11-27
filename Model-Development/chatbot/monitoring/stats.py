"""Profiles of router decisions and their comparison (pure functions, no I/O).

A record is what one answer tells us about the router, from a monitoring event
(api/events.py) or from the router run on a test question (monitoring/baseline.py):
  labels              list of predicted labels
  reply_kind          "medical_refusal", "help", "scope" or None
  unclear             no label passed its threshold
  top_score           highest label score
  unknown_word_share  share of words outside the router's vocabulary (None: no words)
  question_words      number of words

profile() turns records into shares and binned distributions; compare() scores a current
profile against the baseline with the Population Stability Index (PSI) for distributions and
absolute changes for shares.
"""
from __future__ import annotations

import math

from query_router.labels import LABELS

# Fixed bin edges (left-closed, the last bin open), so baseline and live are binned alike.
BINS = {
    "top_score": [0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95],
    "unknown_word_share": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7],
    "question_words": [0, 4, 7, 10, 14, 20, 30],
}
EPS = 1e-4          # empty bins: PSI needs a share > 0 on both sides

RECORD_KEYS = ("labels", "reply_kind", "unclear", "top_score", "unknown_word_share",
               "question_words")


def record_from_event(event: dict) -> dict:
    """The record of one monitoring event (or a Cloud Logging entry holding it)."""
    event = event.get("jsonPayload", event)
    return {k: event.get(k) for k in RECORD_KEYS}


def _bin(value: float, edges: list[float]) -> int:
    i = 0
    while i + 1 < len(edges) and value >= edges[i + 1]:
        i += 1
    return i


def histogram(values: list[float], edges: list[float]) -> list[float]:
    """Share of values per bin; [] if there are no values."""
    values = [v for v in values if v is not None]
    if not values:
        return []
    counts = [0] * len(edges)
    for v in values:
        counts[_bin(v, edges)] += 1
    return [round(c / len(values), 6) for c in counts]


def profile(records: list[dict]) -> dict:
    """Shares and distributions of a set of records."""
    n = len(records)

    def share(pred) -> float | None:
        return round(sum(1 for r in records if pred(r)) / n, 6) if n else None

    return {
        "n": n,
        "label_share": {lbl: share(lambda r, l=lbl: l in (r.get("labels") or []))
                        for lbl in LABELS},
        "refusal_share": share(lambda r: r.get("reply_kind") == "medical_refusal"),
        "unclear_share": share(lambda r: bool(r.get("unclear"))),
        "hist": {name: histogram([r.get(name) for r in records], edges)
                 for name, edges in BINS.items()},
    }


def psi(expected: list[float], actual: list[float]) -> float | None:
    """Population Stability Index of two binned distributions (same bins)."""
    if not expected or not actual or len(expected) != len(actual):
        return None
    total = 0.0
    for e, a in zip(expected, actual):
        e, a = max(e, EPS), max(a, EPS)
        total += (a - e) * math.log(a / e)
    return round(total, 4)


def _level(value: float | None, warn: float, drift: float) -> str:
    if value is None:
        return "n/a"
    return "drift" if value >= drift else "warn" if value >= warn else "ok"


def compare(baseline: dict, current: dict, limits: dict) -> dict:
    """Checks of the current profile against the baseline, and the overall result.

    Returns {"result": "drift" | "warn" | "ok" | "insufficient_data", "n", "checks": [...]};
    each check is {"name", "kind" (psi | share), "baseline", "current", "value", "level"}.
    """
    checks = []
    for name in BINS:
        value = psi(baseline["hist"][name], current["hist"][name])
        checks.append({"name": name, "kind": "psi", "value": value,
                       "level": _level(value, limits["psi_warn"], limits["psi_drift"])})
    shares = [(f"label:{lbl}", baseline["label_share"][lbl], current["label_share"][lbl],
               limits["share_drift"]) for lbl in LABELS]
    shares += [("refusal_share", baseline["refusal_share"], current["refusal_share"],
                limits["refusal_share_drift"]),
               ("unclear_share", baseline["unclear_share"], current["unclear_share"],
                limits["unclear_share_drift"])]
    for name, base, cur, drift in shares:
        value = None if base is None or cur is None else round(abs(cur - base), 4)
        checks.append({"name": name, "kind": "share", "baseline": base, "current": cur,
                       "value": value,
                       "level": _level(value, min(limits["share_warn"], drift), drift)})
    if current["n"] < limits["min_events"]:
        result = "insufficient_data"
    elif any(c["level"] == "drift" for c in checks):
        result = "drift"
    elif any(c["level"] == "warn" for c in checks):
        result = "warn"
    else:
        result = "ok"
    return {"result": result, "n": current["n"], "min_events": limits["min_events"],
            "checks": checks}
