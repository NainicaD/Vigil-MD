"""Candidate pair generation via blocking rules.

Blocking rules (a pair is generated if ANY rule fires):
  1. Normalized manufacturer name matches exactly
  2. Product code matches exactly
  3. Model number token matches exactly

Output:
  data/interim/candidate_pairs.parquet
  data/interim/blocking_report.json

Usage:
    python scripts/matching/blocking.py
"""
from __future__ import annotations

import json
import logging
from itertools import combinations
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("blocking")

INTERIM = Path("data/interim")

try:
    from rapidfuzz.distance import JaroWinkler
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    log.warning("rapidfuzz not installed. Run: pip install rapidfuzz")
    HAS_RAPIDFUZZ = False


def load_all_sources() -> pd.DataFrame:
    dfs = []
    for source in ["enforcement", "recall", "maude", "gudid"]:
        path = INTERIM / f"{source}_flat.parquet"
        df = pd.read_parquet(path)
        dfs.append(df)
    combined = pd.concat(dfs, ignore_index=True)
    log.info("Loaded %d total records across all sources", len(combined))
    return combined


def build_blocks(df: pd.DataFrame) -> dict:
    blocks = {}

    def add(key, idx):
        if key and isinstance(key, str) and len(key) >= 3:
            blocks.setdefault(key, []).append(idx)

    for idx, row in df.iterrows():
        add(f"mfr::{row.get('manufacturer_name_norm')}", idx)
        add(f"pc::{row.get('product_code')}", idx)
        model = row.get("model_number_raw")
        if model and isinstance(model, str):
            model_key = model.strip().upper().replace("-", "").replace(" ", "")
            add(f"model::{model_key}", idx)

    log.info("Built %d blocks", len(blocks))
    return blocks


def generate_pairs(blocks: dict, df: pd.DataFrame) -> list:
    seen = set()
    pairs = []

    for key, indices in blocks.items():
        if len(indices) < 2:
            continue
        for i, j in combinations(indices, 2):
            src_i = df.loc[i, "source"]
            src_j = df.loc[j, "source"]
            if src_i == src_j:
                continue
            pair_key = (min(i, j), max(i, j))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            pairs.append((i, j, key.split("::")[0]))

    log.info("Generated %d unique cross-source candidate pairs", len(pairs))
    return pairs


def safe_similarity(a, b):
    if not HAS_RAPIDFUZZ or not a or not b:
        return 0.0
    return fuzz.token_set_ratio(str(a), str(b)) / 100.0


def jaro_winkler(a, b):
    if not HAS_RAPIDFUZZ or not a or not b:
        return 0.0
    return JaroWinkler.normalized_similarity(str(a), str(b))


def model_key(val):
    if not val or not isinstance(val, str):
        return None
    return val.strip().upper().replace("-", "").replace(" ", "")


def compute_features(row_i: pd.Series, row_j: pd.Series,
                     firing_rule: str) -> dict:
    return {
        "record_id_left":       row_i["source_record_id"],
        "record_id_right":      row_j["source_record_id"],
        "source_left":          row_i["source"],
        "source_right":         row_j["source"],
        "firing_rule":          firing_rule,
        "mfr_exact_match": (
            row_i["manufacturer_name_norm"] == row_j["manufacturer_name_norm"]
            if pd.notna(row_i.get("manufacturer_name_norm"))
               and pd.notna(row_j.get("manufacturer_name_norm"))
            else None
        ),
        "mfr_jaro_winkler":     jaro_winkler(
            row_i.get("manufacturer_name_norm"),
            row_j.get("manufacturer_name_norm")),
        "mfr_token_set_ratio":  safe_similarity(
            row_i.get("manufacturer_name_norm"),
            row_j.get("manufacturer_name_norm")),
        "device_name_similarity": safe_similarity(
            row_i.get("device_name_raw"),
            row_j.get("device_name_raw")),
        "model_exact_match": (
            model_key(row_i.get("model_number_raw")) ==
            model_key(row_j.get("model_number_raw"))
            if row_i.get("model_number_raw") and row_j.get("model_number_raw")
            else None
        ),
        "product_code_match": (
            row_i.get("product_code") == row_j.get("product_code")
            if row_i.get("product_code") and row_j.get("product_code")
            else None
        ),
        "udi_match": (
            row_i.get("udi_di") == row_j.get("udi_di")
            if row_i.get("udi_di") and row_j.get("udi_di")
            else None
        ),
        "left_missing_model":