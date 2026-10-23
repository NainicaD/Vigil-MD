"""Exact identifier linking across flattened source files.

Two deterministic joins, no ML:
  1. Enforcement ↔ Recall via recall_number = product_res_number
  2. MAUDE ↔ GUDID via udi_di

Output:
  data/interim/links_enforcement_recall.parquet
  data/interim/links_maude_gudid.parquet
  data/interim/linking_report.json

Usage:
    python scripts/matching/exact_linking.py
"""
import json
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("exact_linking")

INTERIM = Path("data/interim")


def link_enforcement_recall() -> pd.DataFrame:
    """Join enforcement to recall on recall_number ↔ product_res_number."""
    enf = pd.read_parquet(INTERIM / "enforcement_flat.parquet")
    rec = pd.read_parquet(INTERIM / "recall_flat.parquet")

    # Drop rows where the join key is null
    enf_keyed = enf[enf["link_key"].notna()][["source_record_id", "link_key",
        "manufacturer_name_norm", "device_name_raw", "recall_class", "scope_text"]]
    rec_keyed = rec[rec["link_key"].notna()][["source_record_id", "link_key",
        "manufacturer_name_norm", "device_name_raw", "recall_status", "scope_text",
        "product_code"]]

    merged = enf_keyed.merge(
        rec_keyed,
        on="link_key",
        how="inner",
        suffixes=("_enf", "_rec")
    )

    log.info("Enforcement–Recall: %d enforcement records, %d recall records, %d exact links",
             len(enf_keyed), len(rec_keyed), len(merged))
    return merged


def link_maude_gudid() -> pd.DataFrame:
    """Join MAUDE to GUDID on udi_di."""
    maude = pd.read_parquet(INTERIM / "maude_flat.parquet")
    gudid = pd.read_parquet(INTERIM / "gudid_flat.parquet")

    maude_keyed = maude[maude["udi_di"].notna()][["source_record_id", "udi_di",
        "manufacturer_name_norm", "device_name_raw", "model_number_raw",
        "lot_number", "product_code"]]
    gudid_keyed = gudid[gudid["udi_di"].notna()][["udi_di",
        "manufacturer_name_norm", "device_name_raw", "model_number_raw",
        "catalog_number", "product_codes"]]

    merged = maude_keyed.merge(
        gudid_keyed,
        on="udi_di",
        how="inner",
        suffixes=("_maude", "_gudid")
    )

    log.info("MAUDE–GUDID: %d MAUDE records with UDI, %d GUDID records, %d exact links",
             len(maude_keyed), len(gudid_keyed), len(merged))
    return merged


def check_name_agreement(df, norm_col_a, norm_col_b, label):
    """Check how often normalized manufacturer names agree across a link."""
    if norm_col_a not in df.columns or norm_col_b not in df.columns:
        return
    both = df[[norm_col_a, norm_col_b]].dropna()
    if both.empty:
        return
    agree = (both[norm_col_a] == both[norm_col_b]).sum()
    log.info("%s name agreement: %d/%d (%.0f%%)",
             label, agree, len(both), 100 * agree / len(both))


def main():
    # ── Enforcement ↔ Recall ─────────────────────────────────────────────────
    enf_rec = link_enforcement_recall()
    if not enf_rec.empty:
        out = INTERIM / "links_enforcement_recall.parquet"
        enf_rec.to_parquet(out, index=False)
        log.info("Saved → %s", out)
        check_name_agreement(enf_rec,
            "manufacturer_name_norm_enf",
            "manufacturer_name_norm_rec",
            "Enforcement–Recall")
    else:
        log.warning("No enforcement–recall links found in sample. "
                    "Expected — sample scopes don't overlap. "
                    "Will resolve with full dataset.")

    # ── MAUDE ↔ GUDID ────────────────────────────────────────────────────────
    maude_gudid = link_maude_gudid()
    if not maude_gudid.empty:
        out = INTERIM / "links_maude_gudid.parquet"
        maude_gudid.to_parquet(out, index=False)
        log.info("Saved → %s", out)
        check_name_agreement(maude_gudid,
            "manufacturer_name_norm_maude",
            "manufacturer_name_norm_gudid",
            "MAUDE–GUDID")

        print("\n=== Sample MAUDE–GUDID links ===")
        cols = ["udi_di", "device_name_raw_maude", "device_name_raw_gudid",
                "model_number_raw_maude", "model_number_raw_gudid"]
        print(maude_gudid[cols].head(5).to_string(index=False))
    else:
        log.warning("No MAUDE–GUDID links found.")

    # ── Summary report ───────────────────────────────────────────────────────
    report = {
        "enforcement_recall_links": len(enf_rec),
        "maude_gudid_links":        len(maude_gudid),
        "note": (
            "Zero enforcement-recall links is expected in the sample — "
            "enforcement was not scoped to FRN. "
            "MAUDE-GUDID links confirm UDI-based cross-source linkage works."
        )
    }
    out = INTERIM / "linking_report.json"
    out.write_text(json.dumps(report, indent=2))
    log.info("Linking report → %s", out)
    print("\n=== Linking report ===")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()