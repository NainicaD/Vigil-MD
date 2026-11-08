"""Rules baseline: score each pair with the hand-label guide's decision tree, no training.

It reads only feat_* columns, so it runs on any feature table (the hand sheet now, label_set_v1 later).
Each rule outcome gets a fixed score; a pair counts as MATCH when score >= RULES_CUTOFF.
The scores are ordered so the eval script can also draw a precision-recall curve.

Usage (from anywhere):
    python pipeline/s07_rules_baseline.py
"""
import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

RULES_CUTOFF = 0.75   # fixed by design, not tuned on any labels

CATALOG_KINDS = {"catalog_eq_catalog", "maude_model_eq_gudid_catalog", "maude_catalog_eq_gudid_model"}

parser = argparse.ArgumentParser(description="Rules baseline scores")
parser.add_argument("--features", default=ROOT / "data/hand_features.csv")
parser.add_argument("--out", default=ROOT / "data/scores/rules_hand_scores.csv")
args = parser.parse_args()


def flag(value):
    """True/False from a CSV cell that may say True, False, 1, 0 or be empty."""
    return str(value).strip().upper() in ("TRUE", "1", "1.0")


def rules_score(r):
    """One score per pair, following HAND_LABEL_GUIDE.md section 3, top to bottom."""
    kind = r["feat_match_kind"]

    # step 1: no usable MAUDE identifier -> only a name can agree (E6)
    if flag(r["feat_maude_model_missing"]) and flag(r["feat_maude_catalog_missing"]):
        return 0.2
    # step 2: identifiers conflict, or only a prefix relation (E3b)
    if kind == "conflict":
        return 0.0
    if kind == "prefix_related":
        return 0.3
    # step 3: a more exact sibling exists (E3a)
    if flag(r["feat_conflicting_identifier"]):
        return 0.05

    exact = flag(r["feat_identifier_exact"])
    token = flag(r["feat_token_contains"])
    if not (exact or token):
        return 0.1

    # step 4: non-distinctive key needs a catalog agreement (E4)
    if not flag(r["feat_distinctive"]) and kind not in CATALOG_KINDS:
        return 0.3
    # step 5: same labeler family (E5)
    if flag(r["feat_labeler_equal"]) or flag(r["feat_labeler_alias_equal"]):
        return 0.95 if exact else 0.8
    # names differ, no alias: MATCH only if distinctive and product codes agree
    if flag(r["feat_distinctive"]) and flag(r["feat_product_code_agree"]):
        return 0.75
    return 0.4


features = pd.read_csv(args.features, dtype=str)
scores = pd.DataFrame({
    "pair_id": features["pair_id"],
    "score": features.apply(rules_score, axis=1),
    "model_name": "rules_v1",
    "mlflow_run_id": "none",
    "timestamp": pd.Timestamp.now(tz="UTC").isoformat(),
})
scores.to_csv(args.out, index=False)

accepted = (scores["score"] >= RULES_CUTOFF).sum()
print(f"scored {len(scores)} pairs; {accepted} at or above the cutoff {RULES_CUTOFF}")
print(scores["score"].value_counts().sort_index(ascending=False).to_string())
