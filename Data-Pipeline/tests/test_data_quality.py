"""Ingestion checks — run after every pipeline execution.

Validates the processed Parquet files in data/interim/:
  - Row counts within expected ranges
  - No duplicate source record IDs
  - Required fields are present and not all null
  - Parse failure rate below threshold
  - Schema matches expectations

These run without network access — they check local Parquet files.
"""
import pytest
import pandas as pd
from pathlib import Path

INTERIM = Path("data/interim")

REQUIRED_FIELDS = {
    "enforcement": ["source", "source_record_id", "manufacturer_name_raw",
                    "device_name_raw", "record_date", "recall_class"],
    "recall":      ["source", "source_record_id", "manufacturer_name_raw",
                    "device_name_raw", "record_date", "recall_status"],
    "maude":       ["source", "source_record_id", "manufacturer_name_raw",
                    "device_name_raw", "record_date", "product_code"],
    "gudid":       ["source", "source_record_id", "manufacturer_name_raw",
                    "device_name_raw", "udi_di"],
}

EXPECTED_ROW_RANGES = {
    "enforcement": (1, 500),
    "recall":      (1, 500),
    "maude":       (1, 500),
    "gudid":       (1, 100),
}

NULL_RATE_THRESHOLD = 0.9   # fail if >90% of a required field is null


@pytest.fixture(scope="module")
def flat_tables() -> dict[str, pd.DataFrame]:
    tables = {}
    for source in ["enforcement", "recall", "maude", "gudid"]:
        path = INTERIM / f"{source}_flat.parquet"
        if path.exists():
            tables[source] = pd.read_parquet(path)
    return tables


# ── Row counts ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["enforcement", "recall", "maude", "gudid"])
def test_row_count_in_expected_range(flat_tables, source):
    if source not in flat_tables:
        pytest.skip(f"{source}_flat.parquet not found")
    df = flat_tables[source]
    low, high = EXPECTED_ROW_RANGES[source]
    assert low <= len(df) <= high, \
        f"{source}: {len(df)} rows outside expected range [{low}, {high}]"


# ── Duplicate source IDs ──────────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["enforcement", "recall", "maude", "gudid"])
def test_no_duplicate_source_record_ids(flat_tables, source):
    if source not in flat_tables:
        pytest.skip(f"{source}_flat.parquet not found")
    df = flat_tables[source]
    if "source_record_id" not in df.columns:
        pytest.skip(f"{source} has no source_record_id column")
    dupes = df["source_record_id"].dropna().duplicated().sum()
    assert dupes == 0, \
        f"{source}: {dupes} duplicate source_record_ids found"


# ── Required fields present ───────────────────────────────────────────────────

@pytest.mark.parametrize("source", ["enforcement", "recall", "maude", "gudid"])
def test_required_fields_exist(flat_tables, source):
    if source not in flat_tables:
        pytest.skip(f"{source}_flat.parquet not found")
    df = flat_tables[source]
    for field in REQUIRED_FIELDS[source]:
        assert field in df.columns, \
            f"{source}: required field '{field}' missing from schema"


@pytest.mark.parametrize("source", ["enforcement", "recall", "maude", "gudid"])
def test_required_fields_not_all_null(flat_tables, source):
    if source not in flat_tables:
        pytest.skip(f"{source}_flat.parquet not found")
    df = flat_tables[source]
    for field in REQUIRED_FIELDS[source]:
        if field not in df.columns:
            continue
        null_rate = df[field].isna().mean()
        assert null_rate < NULL_RATE_THRESHOLD, \
            f"{source}.{field}: {null_rate:.0%} null — exceeds threshold"


# ── Parse failure rate ────────────────────────────────────────────────────────

def test_maude_udi_coverage(flat_tables):
    """At least 40% of MAUDE records should have a udi_di (we saw 55%)."""
    if "maude" not in flat_tables:
        pytest.skip("maude_flat.parquet not found")
    df = flat_tables["maude"]
    if "udi_di" not in df.columns:
        pytest.skip("udi_di column not present")
    coverage = df["udi_di"].notna().mean()
    assert coverage >= 0.4, \
        f"MAUDE UDI coverage {coverage:.0%} below 40% threshold"


def test_manufacturer_norm_coverage(flat_tables):
    """At least 50% of records should have a normalized manufacturer name."""
    for source, df in flat_tables.items():
        if "manufacturer_name_norm" not in df.columns:
            continue
        coverage = df["manufacturer_name_norm"].notna().mean()
        assert coverage >= 0.5, \
            f"{source}: manufacturer_name_norm coverage {coverage:.0%} below 50%"


def test_record_date_coverage(flat_tables):
    """At least 75% of records should have a parseable record date.
    Note: recall records have ~79% coverage because event_date_posted
    is not always populated in the FDA recall database.
    """
    thresholds = {
        "enforcement": 0.80,
        "recall":      0.75,   # FDA recall db has incomplete date coverage
        "maude":       0.80,
        "gudid":       0.80,
    }
    for source, df in flat_tables.items():
        if "record_date" not in df.columns:
            continue
        coverage = df["record_date"].notna().mean()
        threshold = thresholds.get(source, 0.80)
        assert coverage >= threshold, \
            f"{source}: record_date coverage {coverage:.0%} below {threshold:.0%}"


# Schema-shape checks (source column, raw_json, links file) live in
# test_data_contract.py.
