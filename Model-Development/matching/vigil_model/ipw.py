"""Inverse-probability weights from the data pipeline's bias report, mapped onto training rows.

Data-Pipeline/reports/bias/mitigation_weights.csv is keyed event_type x manufacturer_tier x year_group.
Training rows have no event_type, so the weights are collapsed to tier x year_group (mean of
weight_full_trimmed, weighted by n_linked) and joined on meta_mfr_tier and meta_year.
"""
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS_CSV = ROOT.parent.parent / "Data-Pipeline/reports/bias/mitigation_weights.csv"

# training tiers the bias report does not name
TIER_ALIASES = {"smaller": "long_tail", "mfr_missing": "(null)"}


def year_group(year) -> str:
    """The bias report's year buckets; anything else is 'unknown'."""
    y = pd.to_numeric(year, errors="coerce")
    if pd.isna(y):
        return "unknown"
    for lo, hi in ((2016, 2017), (2018, 2020), (2021, 2023), (2024, 2026)):
        if lo <= y <= hi:
            return f"{lo}_{hi}"
    return "unknown"


def cell_weights(path=WEIGHTS_CSV) -> dict[tuple[str, str], float]:
    """(tier, year_group) -> weight_full_trimmed averaged over event types, weighted by n_linked."""
    w = pd.read_csv(path, dtype={"manufacturer_tier": str, "year_group": str})
    w = w[w["n_linked"] > 0]
    out = {}
    for key, g in w.groupby(["manufacturer_tier", "year_group"]):
        out[key] = float(np.average(g["weight_full_trimmed"], weights=g["n_linked"]))
    return out


def row_weights(df: pd.DataFrame, path=WEIGHTS_CSV) -> np.ndarray:
    """One weight per row, scaled to mean 1. A cell the report lacks (or a table without the
    columns) gets 1.0 before scaling."""
    cells = cell_weights(path)
    if "meta_mfr_tier" not in df.columns:
        return np.ones(len(df))
    tiers = df["meta_mfr_tier"].astype(str).map(lambda t: TIER_ALIASES.get(t, t))
    years = df["meta_year"].map(year_group) if "meta_year" in df.columns else pd.Series("unknown", index=df.index)
    w = np.array([cells.get((t, y), 1.0) for t, y in zip(tiers, years)], dtype=float)
    return w / w.mean()
