"""Write a small synthetic feature table for the CI smoke training run. No GCP, no real data.

Pull requests get no GCP credentials, so CI checks the training code (scripts/s06_train_models.py)
on made-up rows. They have exactly the columns s06 reads: label, meta_split, meta_device_key,
meta_mfr_tier and every FEATURE_COLUMNS column, with the same value types the real table has
(True/False, numbers, empty for unknown, a MatchKind value). Labels depend on the features, so the
models have something to learn. The scores mean nothing and are never reported.

If FEATURE_COLUMNS gains a column this file does not know, it stops with an error instead of
guessing, so a change to the feature contract is noticed here first.

Usage (from Model-Development/matching/):
    python ci/make_ci_feature_table.py --out data/ci/features.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model.same_device_features import FEATURE_COLUMNS, MatchKind, assert_no_leakage  # noqa: E402

EXACT_KINDS = [MatchKind.SAME_MODEL_KEY, MatchKind.MAUDE_MODEL_EQ_GUDID_CATALOG,
               MatchKind.MAUDE_CATALOG_EQ_GUDID_MODEL, MatchKind.CATALOG_EQ_CATALOG]
CROSS_KINDS = [MatchKind.MAUDE_MODEL_EQ_GUDID_CATALOG, MatchKind.MAUDE_CATALOG_EQ_GUDID_MODEL]
OTHER_KINDS = [MatchKind.CONFLICT, MatchKind.CONFLICT, MatchKind.CONFLICT, MatchKind.PREFIX_RELATED,
               MatchKind.CONTAINS, MatchKind.MAUDE_MODEL_MISSING_OR_PLACEHOLDER, MatchKind.GUDID_MODEL_MISSING]
TIERS = ["top_10", "11_100", "smaller"]


def make(n_devices: int = 600, rows_per_device: int = 5, seed: int = 7) -> pd.DataFrame:
    """Rows grouped by device, 20% of devices in test, about 42% MATCH (like label_set_v1)."""
    rng = np.random.default_rng(seed)
    n = n_devices * rows_per_device
    device = np.repeat([f"ci labeler {i % 40}|CI{i:05d}" for i in range(n_devices)], rows_per_device)
    split = np.repeat(np.where(rng.random(n_devices) < 0.2, "test", "train"), rows_per_device)
    match = rng.random(n) < 0.42

    def chance(p_match: float, p_non: float) -> np.ndarray:
        return rng.random(n) < np.where(match, p_match, p_non)

    exact = chance(0.85, 0.05)
    kind = np.where(exact, rng.choice([k.value for k in EXACT_KINDS], n),
                    rng.choice([k.value for k in OTHER_KINDS], n))
    code_agree = np.where(rng.random(n) < 0.1, "", np.where(chance(0.9, 0.5), "True", "False"))
    values = {
        "feat_match_kind": kind,
        "feat_identifier_exact": exact,
        "feat_identifier_cross_field": np.isin(kind, [k.value for k in CROSS_KINDS]),
        "feat_raw_identifier_equal": exact & (rng.random(n) < 0.6),
        "feat_token_contains": chance(0.3, 0.05),
        "feat_prefix_related": kind == MatchKind.PREFIX_RELATED.value,
        "feat_conflicting_identifier": ~match & (rng.random(n) < 0.1),
        "feat_distinctive": rng.random(n) < 0.9,
        "feat_n_dis_for_model": rng.integers(1, 40, n),
        "feat_labeler_equal": chance(0.6, 0.7),
        "feat_labeler_alias_equal": chance(0.2, 0.05),
        "feat_labeler_missing": rng.random(n) < 0.02,
        "feat_product_code_agree": code_agree,
        "feat_maude_model_missing": kind == MatchKind.MAUDE_MODEL_MISSING_OR_PLACEHOLDER.value,
        "feat_maude_catalog_missing": rng.random(n) < 0.5,
        "feat_gudid_model_missing": kind == MatchKind.GUDID_MODEL_MISSING.value,
        "feat_device_name_token_overlap": np.round(np.where(match, rng.beta(5, 2, n), rng.beta(2, 5, n)), 4),
        "feat_maude_name_has_kit_word": rng.random(n) < 0.1,
        "feat_gudid_name_has_kit_word": rng.random(n) < 0.1,
        # F2: empty (as in the CSV of the real table) unless the kind is contains
        "feat_contains_extra_len": np.where(kind == MatchKind.CONTAINS.value,
                                            rng.integers(1, 10, n).astype(str), ""),
        "feat_contains_at_end": np.where(kind == MatchKind.CONTAINS.value,
                                         np.where(rng.random(n) < 0.5, "True", "False"), ""),
        "feat_text_cosine": np.round(np.where(match, rng.beta(5, 2, n), rng.beta(2, 5, n)), 4),
        "feat_labeler_sim": np.round(np.where(match, rng.beta(4, 2, n), rng.beta(1, 4, n)), 4),
    }
    unknown = [c for c in FEATURE_COLUMNS if c not in values]
    if unknown:
        raise KeyError(f"FEATURE_COLUMNS has columns this generator does not know: {unknown}. "
                       "Add them to ci/make_ci_feature_table.py.")
    df = pd.DataFrame({
        "pair_id": [f"ci{i:06d}" for i in range(n)],
        "label": np.where(match, "MATCH", "NON_MATCH"),
        "meta_split": split,
        "meta_device_key": device,
        "meta_mfr_tier": rng.choice(TIERS, n),
        **{c: values[c] for c in FEATURE_COLUMNS},
    })
    assert_no_leakage(c for c in df.columns if c.startswith("feat_"))
    return df


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, default=ROOT / "data" / "ci" / "features.csv")
    p.add_argument("--devices", type=int, default=600)
    p.add_argument("--rows-per-device", type=int, default=5)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args(argv)
    df = make(args.devices, args.rows_per_device, args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"wrote {len(df)} synthetic rows ({df['meta_split'].eq('train').sum()} train) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
