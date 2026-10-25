"""Flatten raw sample JSON files into clean Parquet files.

What this does per source:
  enforcement — normalize fields, dates, manufacturer name
  recall      — same + extract scope text from code_info
  maude       — explode device[] array (one row per device)
  gudid       — extract fields from nested gudid.device object

Output: data/interim/{source}_flat.parquet

Usage:
    python scripts/preprocessing/flatten_to_parquet.py
"""
import json
import logging
import re
from pathlib import Path
import pandas as pd

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger
log = get_logger("flatten")

SAMPLES = Path("data/raw/samples")
INTERIM = Path("data/interim")
INTERIM.mkdir(parents=True, exist_ok=True)

# ── Normalization helpers ─────────────────────────────────────────────────────

# Legal suffixes to strip from manufacturer names
SUFFIXES = re.compile(
    r",?\s*(inc\.?|incorporated|corp\.?|corporation|llc|ltd\.?|limited"
    r"|co\.?|company|gmbh|s\.a\.?|b\.v\.?)$",
    re.IGNORECASE,
)


def normalize_manufacturer(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes, collapse whitespace."""
    if not name:
        return None
    name = name.strip().lower()
    name = SUFFIXES.sub("", name).strip(" ,.")
    name = re.sub(r"\s+", " ", name)
    return name or None


def normalize_date(val: str) -> str:
    """Convert YYYYMMDD or MM/DD/YYYY to ISO YYYY-MM-DD. Return None if unparseable."""
    if not val or not isinstance(val, str):
        return None
    val = val.strip()
    if re.match(r"^\d{8}$", val):
        return f"{val[:4]}-{val[4:6]}-{val[6:]}"
    if re.match(r"^\d{2}/\d{2}/\d{4}$", val):
        m, d, y = val.split("/")
        return f"{y}-{m}-{d}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", val):
        return val
    return None


def clean(val):
    """Return None for empty strings, empty dicts, empty lists."""
    if val in ("", {}, [], None):
        return None
    return val


# ── Per-source flattening ─────────────────────────────────────────────────────

def flatten_enforcement(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append({
            "source":                "enforcement",
            "source_record_id":      clean(r.get("recall_number")),
            "link_key":              clean(r.get("recall_number")),
            "event_id":              clean(r.get("event_id")),
            "manufacturer_name_raw": clean(r.get("recalling_firm")),
            "manufacturer_name_norm":normalize_manufacturer(r.get("recalling_firm", "")),
            "device_name_raw":       clean(r.get("product_description")),
            "recall_class":          clean(r.get("classification")),
            "reason_text":           clean(r.get("reason_for_recall")),
            "scope_text":            " ".join(filter(None, [
                                         r.get("code_info"),
                                         r.get("more_code_info")])) or None,
            "record_date":           normalize_date(r.get("report_date")),
            "status":                clean(r.get("status")),
            "distribution_pattern":  clean(r.get("distribution_pattern")),
            "raw_json":              json.dumps(r),
        })
    df = pd.DataFrame(rows)
    log.info("enforcement: %d rows, %d columns", len(df), len(df.columns))
    return df


def flatten_recall(records: list) -> pd.DataFrame:
    rows = []
    for r in records:
        rows.append({
            "source":                "recall",
            "source_record_id":      clean(r.get("product_res_number")),
            "link_key":              clean(r.get("product_res_number")),
            "res_event_number":      clean(r.get("res_event_number")),
            "manufacturer_name_raw": clean(r.get("recalling_firm")),
            "manufacturer_name_norm":normalize_manufacturer(r.get("recalling_firm", "")),
            "device_name_raw":       clean(r.get("product_description")),
            "product_code":          clean(r.get("product_code")),
            "reason_text":           clean(r.get("reason_for_recall")),
            "scope_text":            clean(r.get("code_info")),
            "record_date":           normalize_date(r.get("event_date_posted")),
            "recall_status":         clean(r.get("recall_status")),
            "root_cause":            clean(r.get("root_cause_description")),
            "action":                clean(r.get("action")),
            "raw_json":              json.dumps(r),
        })
    df = pd.DataFrame(rows)
    log.info("recall: %d rows, %d columns", len(df), len(df.columns))
    return df


def flatten_maude(records: list) -> pd.DataFrame:
    """Explode device[] array — one row per device per report."""
    rows = []
    for r in records:
        devices = r.get("device", [])
        if not devices:
            # Keep the report even if device array is empty
            devices = [{}]
        for dev in devices:
            rows.append({
                "source":                "maude",
                "source_record_id":      clean(r.get("mdr_report_key")),
                "manufacturer_name_raw": clean(
                    dev.get("manufacturer_d_name") or r.get("manufacturer_name")),
                "manufacturer_name_norm":normalize_manufacturer(
                    dev.get("manufacturer_d_name") or r.get("manufacturer_name", "")),
                "device_name_raw":       clean(dev.get("brand_name")),
                "model_number_raw":      clean(dev.get("model_number")),
                "catalog_number":        clean(dev.get("catalog_number")),
                "lot_number":            clean(dev.get("lot_number")),
                "udi_di":                clean(dev.get("udi_di")),
                "product_code":          clean(dev.get("device_report_product_code")),
                "record_date":           normalize_date(r.get("date_received")),
                "event_type":            clean(r.get("event_type")),
                "adverse_event_flag":    clean(r.get("adverse_event_flag")),
                "report_number":         clean(r.get("report_number")),
                "raw_json":              json.dumps(r),
            })
    df = pd.DataFrame(rows)
    log.info("maude: %d rows (from %d reports), %d columns",
             len(df), len(records), len(df.columns))
    return df


def flatten_gudid(records: list) -> pd.DataFrame:
    """Extract fields from nested gudid.device structure."""
    rows = []
    for r in records:
        dev = r.get("gudid", {}).get("device", {})
        product_codes_raw = r.get("productCodes", [])
        if isinstance(product_codes_raw, dict):
            product_codes_raw = product_codes_raw.get("productCode", [])
        if not isinstance(product_codes_raw, list):
            product_codes_raw = []
        product_codes = [
            pc.get("productCode")
            for pc in product_codes_raw
            if isinstance(pc, dict) and pc.get("productCode")
        ]
        rows.append({
            "source":                "gudid",
            "source_record_id":      clean(r.get("_source_udi_di")),
            "udi_di":                clean(r.get("_source_udi_di")),
            "manufacturer_name_raw": clean(dev.get("companyName")),
            "manufacturer_name_norm":normalize_manufacturer(dev.get("companyName", "")),
            "device_name_raw":       clean(dev.get("brandName")),
            "model_number_raw":      clean(dev.get("versionModelNumber")),
            "catalog_number":        clean(dev.get("catalogNumber")),
            "product_codes":         product_codes or None,
            "device_record_status":  clean(dev.get("deviceRecordStatus")),
            "publish_date":          normalize_date(dev.get("devicePublishDate")) or None,
            "distribution_status":   clean(dev.get("deviceCommDistributionStatus")),
            "raw_json":              json.dumps(r),
        })
    df = pd.DataFrame(rows)
    df["publish_date"] = df["publish_date"].astype("string")
    log.info("gudid: %d rows, %d columns", len(df), len(df.columns))
    return df


# ── Main ──────────────────────────────────────────────────────────────────────

FLATTENERS = {
    "enforcement": flatten_enforcement,
    "recall":      flatten_recall,
    "maude":       flatten_maude,
    "gudid":       flatten_gudid,
}


def main():
    for source, flatten_fn in FLATTENERS.items():
        path = SAMPLES / f"{source}_sample.json"
        records = json.loads(path.read_text()).get("records", [])
        log.info("Processing %s (%d records) …", source, len(records))

        df = flatten_fn(records)

        # Replace empty strings with None across the whole dataframe
        df = df.replace("", None)

        out = INTERIM / f"{source}_flat.parquet"
        df.to_parquet(out, index=False)
        log.info("Saved → %s", out)

    # Quick validation
    print("\n=== Flattening summary ===")
    for source in FLATTENERS:
        df = pd.read_parquet(INTERIM / f"{source}_flat.parquet")
        print(f"\n{source}:")
        print(f"  rows: {len(df)}")
        print(f"  columns: {list(df.columns)}")
        nulls = df.isnull().sum()
        high_null = nulls[nulls > len(df) * 0.5]
        if not high_null.empty:
            print(f"  high-null columns (>50%): {list(high_null.index)}")

    # Check manufacturer normalization
    print("\n=== Sample manufacturer normalization ===")
    for source in ["enforcement", "recall", "maude", "gudid"]:
        df = pd.read_parquet(INTERIM / f"{source}_flat.parquet")
        if "manufacturer_name_raw" in df.columns:
            sample = df[["manufacturer_name_raw", "manufacturer_name_norm"]].dropna().head(3)
            print(f"\n{source}:")
            print(sample.to_string(index=False))


if __name__ == "__main__":
    main()