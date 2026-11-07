"""Flatten raw sample JSON files into clean Parquet files.

What this does per source:
  enforcement — normalize fields, dates, manufacturer name
  recall      — same + extract scope text from code_info
  maude       — explode device[] array (one row per device)
  gudid       — extract fields from nested gudid.device object

Output: data/interim/{source}_flat.parquet

MAUDE event tables (same rules as scripts/warehouse/build_maude_events.py):
  maude_problems.parquet  one row per (report, problem_type, problem_term)
  maude_text.parquet      one row per narrative text, filtered by
                          scripts/warehouse/maude_events_config.py

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
from scripts.warehouse.maude_events_config import TEXT_FILTER
log = get_logger("flatten")

SAMPLES = Path("data/raw/samples")
INTERIM = Path("data/interim")
INTERIM.mkdir(parents=True, exist_ok=True)

# ── Normalization helpers ─────────────────────────────────────────────────────

# Legal suffixes to strip from manufacturer names
# The suffix must not follow a letter or digit, so "Tesco" keeps its "co". The character
# before it is kept: "Gentec(Shanghai)Corporation" -> "gentec(shanghai)".
# Exception: "llc" is also stripped when glued on ("AcumedLLC" -> "acumed"); no word ends in "llc".
SUFFIXES = re.compile(
    r"(^|[^a-z0-9])(inc\.?|incorporated|corp\.?|corporation|llc\.?|ltd\.?|limited"
    r"|co\.?|company|gmbh|s\.a\.?|b\.v\.?)$"
    r"|llc\.?$",
    re.IGNORECASE,
)


def normalize_manufacturer(name: str) -> str:
    """Matching key: lowercase, strip one legal suffix and trailing " ,.", collapse whitespace."""
    if not name:
        return None
    name = name.strip().lower()
    name = SUFFIXES.sub(r"\1", name).strip(" ,.")
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
    # ISO timestamp, e.g. GUDID API '2016-11-16T00:00:00.000Z': keep the date part
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}", val)
    if m:
        return m.group(1)
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


# ── MAUDE event tables ────────────────────────────────────────────────────────

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")


def _terms(values) -> list:
    """Distinct, trimmed, non-empty strings, sorted."""
    return sorted({v.strip() for v in (values or []) if isinstance(v, str) and v.strip()})


def maude_problems(records: list) -> pd.DataFrame:
    """One row per (report, problem_type, problem_term). Device terms come from
    product_problems, patient terms from patient[].patient_problems."""
    rows = []
    for r in records:
        key = clean(r.get("mdr_report_key"))
        if not key:
            continue
        patient_terms = [t for p in (r.get("patient") or []) for t in (p.get("patient_problems") or [])]
        for problem_type, terms in (("device",  _terms(r.get("product_problems"))),
                                    ("patient", _terms(patient_terms))):
            for term in terms:
                rows.append({
                    "mdr_report_key": key,
                    "problem_type":   problem_type,
                    "problem_term":   term,
                    "event_type":     clean(r.get("event_type")),
                    "date_received":  normalize_date(r.get("date_received")),
                })
    df = pd.DataFrame(rows, columns=["mdr_report_key", "problem_type", "problem_term",
                                     "event_type", "date_received"])
    log.info("maude_problems: %d rows from %d reports", len(df), len(records))
    return df


def clean_text(value):
    """Text without control characters (tab and newline kept), trimmed; None if empty.
    Characters that are not valid UTF-8 (for example a lone surrogate from a JSON escape
    like \\ud83d, which Parquet cannot store) become '?'."""
    if not isinstance(value, str):
        return None
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    return CONTROL_CHARS.sub("", value).strip() or None


def maude_text(records: list, filt: dict = TEXT_FILTER) -> pd.DataFrame:
    """One row per narrative text that passes the filter; empty texts dropped,
    one row per (report, mdr_text_key), the longest text kept."""
    best = {}
    for r in records:
        key = clean(r.get("mdr_report_key"))
        received = normalize_date(r.get("date_received"))
        event_type = clean(r.get("event_type"))
        if not key or not received:
            continue
        if not filt["from_year"] <= int(received[:4]) <= filt["to_year"]:
            continue
        if filt.get("event_types") is not None and event_type not in filt["event_types"]:
            continue
        for t in r.get("mdr_text") or []:
            text = clean_text(t.get("text"))
            text_type = clean_text(t.get("text_type_code"))
            if text is None:
                continue
            if filt.get("text_types") is not None and text_type not in filt["text_types"]:
                continue
            row = {
                "mdr_report_key":          key,
                "mdr_text_key":            clean_text(t.get("mdr_text_key")),
                "text_type_code":          text_type,
                "patient_sequence_number": clean_text(t.get("patient_sequence_number")),
                "event_type":              event_type,
                "date_received":           received,
                "date_report":             normalize_date(r.get("date_report")),
                "text":                    text,
            }
            k = (key, row["mdr_text_key"])
            if k not in best or len(text) > len(best[k]["text"]):
                best[k] = row
    df = pd.DataFrame(list(best.values()),
                      columns=["mdr_report_key", "mdr_text_key", "text_type_code",
                               "patient_sequence_number", "event_type", "date_received",
                               "date_report", "text"])
    log.info("maude_text: %d texts from %d reports (filter %s)",
             len(df), df["mdr_report_key"].nunique(), filt)
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

        if source == "maude":
            for name, events_df in (("maude_problems", maude_problems(records)),
                                    ("maude_text",     maude_text(records))):
                out = INTERIM / f"{name}.parquet"
                events_df.astype("string").to_parquet(out, index=False)
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