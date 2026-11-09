"""Bias slices for the router: safety and accuracy per group of questions.

The course guidelines ask for slices even for parts that are not trained on
user data. For every slice field present in the test rows (audience,
device_type, event_type, style) and every value of it:
  advice_recall       medical-advice questions refused (safety)
  false_refusal_rate  in-scope questions refused by mistake (cost)
  exact_match         all labels right (accuracy)
  answered            in-scope questions that get at least one factual query
For each field and metric, the gap is the largest minus the smallest value
over slices with at least MIN_N questions in that metric's denominator. A gap
above GAP_LIMITS is flagged; gate.py uses the same limits.
"""

import numpy as np

from query_router import labels

SLICE_FIELDS = ('audience', 'device_type', 'event_type', 'style')
MIN_N = 8  # Smaller slices are reported but not used for gaps.
GAP_LIMITS = {'advice_recall': 0.15, 'false_refusal_rate': 0.25}
OOS = labels.LABELS.index(labels.OUT_OF_SCOPE)
FACTUAL = [i for i, name in enumerate(labels.LABELS)
           if name != labels.OUT_OF_SCOPE]


def _rate(values: np.ndarray, mask: np.ndarray) -> tuple[float | None, int]:
    n = int(mask.sum())
    return (round(float(values[mask].mean()), 4) if n else None), n


def slice_metrics(y: np.ndarray, pred: np.ndarray, rows: list[dict],
                  idx: np.ndarray) -> dict:
    """Returns the four metrics and their denominators for rows idx.

    Args:
      y: True labels, one 0/1 row per question (labels.LABELS order).
      pred: Predicted labels after the rule backstop.
      rows: The questions (oos_kind marks advice rows).
      idx: Boolean mask of the rows in this slice.
    """
    advice = idx & np.array([r.get('oos_kind') == 'advice' for r in rows])
    in_scope = idx & (y[:, OOS] == 0)
    flagged = pred[:, OOS] == 1
    exact = (pred == y).all(axis=1)
    answered = pred[:, FACTUAL].any(axis=1)
    out = {'n': int(idx.sum())}
    out['advice_recall'], out['n_advice'] = _rate(flagged, advice)
    out['false_refusal_rate'], out['n_in_scope'] = _rate(flagged, in_scope)
    out['exact_match'], _ = _rate(exact, idx)
    out['answered'], _ = _rate(answered, in_scope)
    return out


def gaps(slices: dict[str, dict], min_n: int = MIN_N) -> dict:
    """Returns, per metric, the gap between the best and worst large slice.

    Args:
      slices: Slice value -> slice_metrics() output, for one field.
      min_n: Slices with fewer questions in the metric's denominator are left
        out.

    Returns:
      Metric -> {gap, worst, best} (gap None when fewer than two slices count).
    """
    denominators = {'advice_recall': 'n_advice',
                    'false_refusal_rate': 'n_in_scope',
                    'exact_match': 'n', 'answered': 'n_in_scope'}
    out = {}
    for metric, n_key in denominators.items():
        vals = {k: s[metric] for k, s in slices.items()
                if s[metric] is not None and s[n_key] >= min_n}
        if len(vals) < 2:
            out[metric] = {'gap': None, 'worst': None, 'best': None}
            continue
        higher_is_worse = metric == 'false_refusal_rate'
        ranked = sorted(vals, key=vals.get, reverse=higher_is_worse)
        out[metric] = {'gap': round(max(vals.values()) - min(vals.values()), 4),
                       'worst': ranked[0], 'best': ranked[-1]}
    return out


def slice_report(y: np.ndarray, pred: np.ndarray, rows: list[dict],
                 fields=SLICE_FIELDS) -> dict:
    """Returns slices, gaps and flags for every field present in rows.

    Args:
      y: True labels.
      pred: Predicted labels after the rule backstop.
      rows: The questions with their slice fields.
      fields: Slice fields to look for.

    Returns:
      {field: {"slices": {value: metrics}, "gaps": ..., "flags": [...]}},
      where flags lists the metrics whose gap exceeds GAP_LIMITS.
    """
    report = {}
    for field in fields:
        values = sorted({r[field] for r in rows if r.get(field)})
        if not values:
            continue
        column = np.array([r.get(field) for r in rows])
        slices = {v: slice_metrics(y, pred, rows, column == v) for v in values}
        field_gaps = gaps(slices)
        flags = [m for m, limit in GAP_LIMITS.items()
                 if field_gaps[m]['gap'] is not None
                 and field_gaps[m]['gap'] > limit]
        report[field] = {'slices': slices, 'gaps': field_gaps, 'flags': flags}
    return report


def max_gap(report: dict, metric: str) -> float | None:
    """Returns the largest gap of a metric over all fields of a report."""
    found = [f['gaps'][metric]['gap'] for f in report.values()
             if f['gaps'][metric]['gap'] is not None]
    return max(found) if found else None
