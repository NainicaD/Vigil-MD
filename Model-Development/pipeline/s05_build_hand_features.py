"""Compute the shared features (Hitika's pair_features) for the hand-label sheet.

Reads the labeled sheet, keeps candidate rows (cand_rank 1-3), builds both sides of each pair,
calls pair_features, and writes one row per candidate with the feat_* columns.

Runs on the local CSV only: no BigQuery.

Usage (from anywhere):
    python pipeline/s05_build_hand_features.py --sheet /tmp/hand_labels.csv
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model.rows import is_true, load_aliases, number, product_codes, text  # noqa: E402
from vigil_model.same_device_features import (  # noqa: E402
    FEATURE_COLUMNS, DeviceSide, MatchContext, assert_no_leakage, pair_features, usable_key,
)

parser = argparse.ArgumentParser(description="Features for the hand-label sheet")
parser.add_argument("--sheet", default=ROOT / "labels/hand_label_sample_v1.csv")
parser.add_argument("--alias", default=ROOT / "labels/alias_table_v1.csv")
parser.add_argument("--out", default=ROOT / "data/hand_features.csv")
args = parser.parse_args()


# read everything as text, so model numbers like 007 or 1006402 stay exactly as written
sheet = pd.read_csv(args.sheet, dtype=str)
cands = sheet[sheet["cand_rank"].astype(int) > 0].copy()
print(f"sheet rows: {len(sheet)}, candidate rows: {len(cands)}")

aliases = load_aliases(args.alias)
assert_no_leakage(FEATURE_COLUMNS)

rows = []
for _, r in cands.iterrows():
    maude = DeviceSide(
        manufacturer_norm=text(r["maude_manufacturer_norm"]),
        device_name=text(r["maude_device_name"]),
        model_raw=text(r["maude_model_raw"]),
        catalog_raw=text(r["maude_catalog_raw"]),
        product_codes=product_codes(r["maude_product_code"]),
    )
    gudid = DeviceSide(
        manufacturer_norm=text(r["cand_labeler"]),
        device_name=text(r["cand_device_name"]),
        model_raw=text(r["cand_model"]),
        catalog_raw=text(r["cand_catalog"]),
        product_codes=product_codes(r["cand_product_codes"]),
    )
    # APPROXIMATION: the sheet has no sibling key list, only cand_has_suffix_sibling.
    # When it is TRUE we treat the MAUDE keys as existing siblings, so the E3a check can fire.
    # Confirm with Hitika that this matches how the label set computes it.
    if is_true(r["cand_has_suffix_sibling"]):
        siblings = frozenset(k for k in (usable_key(maude.model_raw), usable_key(maude.catalog_raw)) if k)
    else:
        siblings = frozenset()

    ctx = MatchContext(alias_pairs=aliases, n_dis_for_model=number(r["cand_n_dis_for_model"]),
                       sibling_keys=siblings)
    feats = pair_features(maude, gudid, ctx)
    rows.append({"pair_id": r["pair_id"], **feats})

features = pd.DataFrame(rows)
keep = ["pair_id", "maude_row_id", "cand_rank", "label", "cand_split", "meta_sample_weight",
        "hard_case_reason", "strat_model_usable", "strat_mfr_size", "cand_match_kind"]
out = cands[keep].merge(features, on="pair_id", how="inner")
assert len(out) == len(cands), "lost rows while joining features"

# parity check: our match kind should equal the one the sheet was built with
same = (out["feat_match_kind"] == out["cand_match_kind"]).mean()
print(f"match kind agrees with the sheet on {same:.1%} of rows")
if same < 1:
    diff = out[out["feat_match_kind"] != out["cand_match_kind"]]
    print(diff[["pair_id", "cand_match_kind", "feat_match_kind"]].head(10).to_string(index=False))

out.to_csv(args.out, index=False)
print(f"saved {len(out)} rows with {len(FEATURE_COLUMNS)} features to {args.out}")
