"""Compute the shared features (Hitika's pair_features) for the hand-label sheet.

Reads the labeled sheet, keeps candidate rows (cand_rank 1-3), builds both sides of each pair,
calls pair_features, and writes one row per candidate with the feat_* columns.

Runs on the local CSV only: no BigQuery.

Usage (from Model-Development/eval):
    python hand_label_features.py --sheet /tmp/hand_labels.csv
"""
import argparse
import re
import sys

import pandas as pd

sys.path.append("../scripts")
from same_device_features import (  # noqa: E402
    FEATURE_COLUMNS, DeviceSide, MatchContext, assert_no_leakage, pair_features, usable_key,
)

parser = argparse.ArgumentParser(description="Features for the hand-label sheet")
parser.add_argument("--sheet", default="../labels/hand_label_sample_v1.csv")
parser.add_argument("--alias", default="../labels/alias_table_v1.csv")
parser.add_argument("--out", default="hand_features.csv")
args = parser.parse_args()


def text(value):
    """A clean string, or None. Treats empty cells and NaN as missing."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    value = str(value).strip()
    return value or None


def number(value):
    """An int, or None."""
    value = text(value)
    return None if value is None else int(float(value))


def is_true(value):
    return text(value) is not None and text(value).upper() in ("TRUE", "1", "YES")


def product_codes(value):
    """FDA product codes are 3 letters; pull every one out, whatever the separator."""
    value = text(value)
    return tuple(re.findall(r"[A-Z]{3}", value.upper())) if value else ()


def load_aliases(path):
    """Alias pairs (maude labeler, gudid labeler) from the train-only alias table."""
    table = pd.read_csv(path, dtype=str)
    known = [("maude_labeler", "gudid_labeler"),
             ("maude_manufacturer_norm", "gudid_manufacturer_norm"),
             ("maude_name", "gudid_name")]
    for m_col, g_col in known:
        if m_col in table.columns and g_col in table.columns:
            break
    else:
        m_col, g_col = table.columns[0], table.columns[1]
    print(f"alias table: {len(table)} pairs, using columns {m_col!r} -> {g_col!r}")
    return frozenset((text(m), text(g)) for m, g in zip(table[m_col], table[g_col]) if text(m) and text(g))


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
