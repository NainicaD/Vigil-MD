"""Evaluate a model's scores on the hand-labeled sheet (HAND_LABEL_GUIDE.md section 9).

- candidate rows only (cand_rank 1-3); rank 0 is used only for retrieval recall
- INSUFFICIENT_EVIDENCE left out of precision and recall, reported as a count and an accept rate
- every number twice: unweighted (the sample) and weighted by meta_sample_weight (the no-UDI pool)
- two scopes: all candidate rows, and strict (cand_split = test)
- precision comes with a 95% lower bound and the count of accepted pairs behind it
- the cutoff must come from validation; it is never chosen here (plan rule 1)

Usage (from anywhere):
    python eval/s08_evaluate_hand.py --scores data/scores/rules_hand_scores.csv \
        --cutoff 0.75 --track baseline
"""
import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd
from scipy.stats import beta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model.same_device_features import insufficient_evidence  # noqa: E402
from vigil_model.tracking import lineage_tags, tracking_uri  # noqa: E402

parser = argparse.ArgumentParser(description="Evaluate scores on the hand-labeled sheet")
parser.add_argument("--scores", required=True)
parser.add_argument("--cutoff", type=float, required=True, help="chosen on validation, never on these labels")
parser.add_argument("--sheet", default=ROOT / "labels/hand_label_sample_v1.csv")
parser.add_argument("--manifest", default=ROOT / "labels/label_set_v1.manifest.json")
parser.add_argument("--track", default="test", choices=["test", "baseline", "xgb", "embed", "ft"])
parser.add_argument("--threshold-version", default="t1")
parser.add_argument("--normalization-version", default="n1")
parser.add_argument("--ie-gate", action="store_true",
                    help="E4: rows the IE gate catches are never accepted (same_device_features.insufficient_evidence)")
parser.add_argument("--hand-features", default=ROOT / "data/hand_features.csv", help="feat_* columns for --ie-gate")
parser.add_argument("--exp", help="experiment id from docs/EXPERIMENTS.md (default: $VIGIL_EXP)")
parser.add_argument("--no-mlflow", action="store_true")
args = parser.parse_args()


def precision_lower_bound(correct, accepted):
    """One-sided 95% lower bound on precision (Clopper-Pearson). With 0 errors in 44, about 0.93."""
    if accepted == 0:
        return float("nan")
    return float(beta.ppf(0.05, correct, accepted - correct + 1)) if correct > 0 else 0.0


def metrics(df):
    """Precision and recall at the cutoff, unweighted and weighted, with the counts behind them."""
    pred = df["score"] >= args.cutoff
    y = df["y"] == 1
    w = df["w"]
    accepted, correct, matches = int(pred.sum()), int((pred & y).sum()), int(y.sum())
    w_acc, w_cor, w_mat = w[pred].sum(), w[pred & y].sum(), w[y].sum()
    return {
        "pairs": len(df),
        "matches": matches,
        "accepted": accepted,
        "false_pos": accepted - correct,
        "precision": correct / accepted if accepted else float("nan"),
        "precision_lower95": precision_lower_bound(correct, accepted),
        "recall": correct / matches if matches else float("nan"),
        "precision_weighted": w_cor / w_acc if w_acc else float("nan"),
        "recall_weighted": w_cor / w_mat if w_mat else float("nan"),
    }


def show(title, m):
    print(f"\n{title}")
    print(f"  pairs={m['pairs']}  true matches={m['matches']}  accepted={m['accepted']}  "
          f"false positives={m['false_pos']}")
    print(f"  precision={m['precision']:.3f} (95% lower bound {m['precision_lower95']:.3f})  "
          f"recall={m['recall']:.3f}")
    print(f"  weighted: precision={m['precision_weighted']:.3f}  recall={m['recall_weighted']:.3f}")


# --- load and join -------------------------------------------------------------------------------
sheet = pd.read_csv(args.sheet, dtype=str)
sheet["cand_rank"] = sheet["cand_rank"].astype(int)
scores = pd.read_csv(args.scores, dtype={"pair_id": str})
model_name = scores["model_name"].iloc[0]

cands = sheet[sheet["cand_rank"] > 0].merge(scores[["pair_id", "score"]], on="pair_id", how="left")
missing = cands["score"].isna().sum()
assert missing == 0, f"{missing} candidate rows have no score"

unlabeled = cands["label"].isna().sum()
assert unlabeled == 0, f"{unlabeled} candidate rows are not labeled yet"

gated = 0
if args.ie_gate:
    feats = pd.read_csv(args.hand_features, dtype=str).set_index("pair_id")
    absent = set(cands["pair_id"]) - set(feats.index)
    assert not absent, f"{len(absent)} candidate rows have no features in {args.hand_features}"
    gate = cands["pair_id"].map(lambda p: insufficient_evidence(feats.loc[p]))
    cands.loc[gate, "score"] = float("-inf")   # routed to IE: below any cutoff, still counts as a missed MATCH
    gated = int(gate.sum())
    print(f"IE gate caught {gated} candidate rows: {cands.loc[gate, 'label'].value_counts().to_dict()}")

ie = cands[cands["label"] == "INSUFFICIENT_EVIDENCE"]
scored = cands[cands["label"].isin(["MATCH", "NON_MATCH"])].copy()
scored["y"] = (scored["label"] == "MATCH").astype(int)
scored["w"] = scored["meta_sample_weight"].astype(float)
strict = scored[scored["cand_split"] == "test"]

print(f"model: {model_name}   cutoff: {args.cutoff}")
print(f"candidate rows: {len(cands)}   INSUFFICIENT_EVIDENCE left out: {len(ie)}   used: {len(scored)}")
# IE rows are out of precision, but accepting them is still a wrong link (MODEL_IMPROVEMENT change 4)
ie_accepted = int((ie["score"] >= args.cutoff).sum())
ie_accept_rate = ie_accepted / len(ie) if len(ie) else float("nan")
print(f"INSUFFICIENT_EVIDENCE accepted at the cutoff: {ie_accepted}/{len(ie)} ({ie_accept_rate:.3f})")
print("NOTE: every row says labeler=hitika; rows labeled by formula or Claude are not marked yet,")
print("      so these numbers are not yet against human-only ground truth.")

m_all = metrics(scored)
m_strict = metrics(strict)
show("ALL candidate rows", m_all)
show("STRICT (cand_split = test)", m_strict)

# --- retrieval recall@1: does the top-scored candidate equal a true match? ------------------------
rank0 = sheet[sheet["cand_rank"] == 0][["maude_row_id", "label"]]
has_true = rank0[rank0["label"] == "NON_MATCH"]["maude_row_id"]   # "none of these" is false
top = cands.sort_values("score", ascending=False).groupby("maude_row_id").head(1)
top = top[top["maude_row_id"].isin(has_true)]
retrieval_at1 = (top["label"] == "MATCH").mean() if len(top) else float("nan")
print(f"\nretrieval recall@1: {retrieval_at1:.3f} over {len(top)} MAUDE rows that have a true candidate")

# --- slices (bias check) and hard cases ------------------------------------------------------------
slice_metrics = {}
for col in ["strat_model_usable", "strat_mfr_size"]:
    print(f"\nby {col} (all rows, unweighted):")
    for value, group in scored.groupby(col):
        m = metrics(group)
        slice_metrics[f"{col}_{value}"] = m
        print(f"  {value:<10} pairs={m['pairs']:<4} matches={m['matches']:<4} accepted={m['accepted']:<4} "
              f"precision={m['precision']:.3f} recall={m['recall']:.3f}")

print("\nhard cases (examples, too few for percentages):")
hard = scored[scored["hard_case_reason"].fillna("none") != "none"]
for reason, group in hard.groupby("hard_case_reason"):
    m = metrics(group)
    print(f"  {reason:<20} pairs={m['pairs']:<3} matches={m['matches']:<3} "
          f"accepted={m['accepted']:<3} false positives={m['false_pos']}")

false_pos = scored[(scored["score"] >= args.cutoff) & (scored["y"] == 0)]
if len(false_pos):
    print("\nfalse positives (for error analysis):")
    cols = ["pair_id", "hard_case_reason", "cand_match_kind", "score", "cand_split"]
    print(false_pos[cols].to_string(index=False))

# --- MLflow --------------------------------------------------------------------------------------
if args.no_mlflow:
    print("\nSkipped MLflow logging")
else:
    import mlflow

    manifest = {}
    if os.path.exists(args.manifest):
        with open(args.manifest) as f:
            manifest = json.load(f)

    flat = {"ie_count": len(ie), "ie_accepted": ie_accepted, "ie_accept_rate": ie_accept_rate, "ie_gated": gated,
            "retrieval_recall_at1": retrieval_at1}
    for scope, m in (("all", m_all), ("strict", m_strict)):
        for k, v in m.items():
            flat[f"{scope}_{k}"] = v
    for name, m in slice_metrics.items():
        flat[f"slice_{name}_precision"] = m["precision"]
        flat[f"slice_{name}_recall"] = m["recall"]
        flat[f"slice_{name}_accepted"] = m["accepted"]
    flat = {k: float(v) for k, v in flat.items() if pd.notna(v)}

    mlflow.set_tracking_uri(tracking_uri())
    mlflow.set_experiment("vigil-md-linkage")
    with mlflow.start_run(run_name=f"{model_name}_hand"):
        mlflow.set_tags({
            **lineage_tags(args.manifest, args.exp),
            "ie_gate": "e6_e3b" if args.ie_gate else "off",
            "track": args.track, "model_name": model_name, "eval_set": "hand_label_sample_v1",
            "threshold_version": args.threshold_version, "normalization_version": args.normalization_version,
            "constants_sha256": manifest.get("constants_sha256", "unknown"),
            "alias_table_sha256": manifest.get("alias_table_sha256", "unknown"),
            "snapshot_date": manifest.get("snapshot_date", "unknown"),
            "label_source_note": "labeler column does not separate formula/Claude rows",
        })
        mlflow.log_params({"cutoff": args.cutoff, "scores_file": args.scores, "sheet": args.sheet})
        mlflow.log_metrics(flat)
    print("\nLogged to MLflow")
