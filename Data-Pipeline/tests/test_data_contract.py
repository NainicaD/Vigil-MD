"""Contract tests: data/interim/*.parquet must be loadable by BigQuery.

Runs locally with pyarrow only. No GCP calls. Checks the real files that
load_interim_to_bq.py uploads against BigQuery's Parquet load rules.
"""
import json
import re
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.acquisition.load_interim_to_bq import SOURCES

INTERIM = Path(__file__).resolve().parents[1] / "data" / "interim"

FLAT = {s: INTERIM / f"{s}_flat.parquet" for s in SOURCES}
LINKS = INTERIM / "links_maude_gudid.parquet"
ALL_FILES = {**FLAT, "links_maude_gudid": LINKS}

REQUIRED_COLUMNS = {
    "enforcement": {"source", "source_record_id", "link_key", "manufacturer_name_raw",
                    "manufacturer_name_norm", "raw_json"},
    "recall": {"source", "source_record_id", "link_key", "manufacturer_name_raw",
               "manufacturer_name_norm", "raw_json"},
    "maude": {"source", "source_record_id", "manufacturer_name_norm", "udi_di", "raw_json"},
    "gudid": {"source", "source_record_id", "udi_di", "manufacturer_name_norm", "raw_json"},
    "links_maude_gudid": {"source_record_id", "udi_di"},
}

BQ_COLUMN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,299}$")
BQ_RESERVED_PREFIXES = ("_TABLE_", "_FILE_", "_PARTITION", "_ROW_TIMESTAMP",
                        "_COLIDENTIFIER", "_CHANGE_SEQUENCE_NUMBER", "_CHANGE_TYPE")


@pytest.fixture(scope="module", params=sorted(ALL_FILES))
def table(request):
    name = request.param
    path = ALL_FILES[name]
    if not path.exists():
        pytest.skip(f"{path} not generated")
    return name, path, pq.read_table(path)


def is_bq_supported(t: pa.DataType, in_list: bool = False) -> bool:
    if pa.types.is_null(t):
        return False
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        # BigQuery has no nested arrays (ARRAY<ARRAY<..>>)
        return not in_list and is_bq_supported(t.value_type, in_list=True)
    if pa.types.is_struct(t):
        return all(is_bq_supported(f.type) for f in t)
    if pa.types.is_unsigned_integer(t):
        return not pa.types.is_uint64(t)
    if pa.types.is_timestamp(t):
        return t.unit != "ns"
    if pa.types.is_map(t) or pa.types.is_union(t) or pa.types.is_dictionary(t):
        return False
    return (pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_boolean(t)
            or pa.types.is_string(t) or pa.types.is_large_string(t)
            or pa.types.is_binary(t) or pa.types.is_date(t) or pa.types.is_time(t)
            or pa.types.is_decimal(t))


# ── file integrity ───────────────────────────────────────────────────────────

def test_file_is_valid_parquet_with_rows(table):
    name, path, t = table
    assert t.num_rows > 0, f"{name}: no rows (BigQuery load would give an empty table)"


def test_metadata_row_count_matches_rows_read(table):
    name, path, t = table
    assert pq.ParquetFile(path).metadata.num_rows == t.num_rows


# ── BigQuery schema rules ───────────────────────────────────────────────────

def test_column_names_are_legal_for_bigquery(table):
    name, _, t = table
    bad = [c for c in t.column_names if not BQ_COLUMN_NAME.match(c)]
    assert not bad, f"{name}: illegal BigQuery column names {bad}"


def test_no_reserved_column_prefixes(table):
    name, _, t = table
    bad = [c for c in t.column_names if c.upper().startswith(BQ_RESERVED_PREFIXES)]
    assert not bad, f"{name}: reserved column prefixes {bad}"


def test_no_duplicate_column_names_case_insensitive(table):
    name, _, t = table
    lowered = [c.lower() for c in t.column_names]
    assert len(lowered) == len(set(lowered)), f"{name}: duplicate columns"


def test_column_types_are_supported_by_bigquery(table):
    name, _, t = table
    bad = {f.name: str(f.type) for f in t.schema if not is_bq_supported(f.type)}
    assert not bad, f"{name}: column types BigQuery cannot load: {bad}"


# ── schema contract ─────────────────────────────────────────────────────────

def test_required_columns_present(table):
    name, _, t = table
    missing = REQUIRED_COLUMNS[name] - set(t.column_names)
    assert not missing, f"{name}: missing required columns {missing}"


def test_source_record_id_never_null_or_empty(table):
    name, _, t = table
    ids = t.column("source_record_id").to_pylist()
    bad = [i for i, v in enumerate(ids) if v is None or str(v).strip() == ""]
    assert not bad, f"{name}: null/empty source_record_id at rows {bad[:10]}"


# ── content contract ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", SOURCES)
def test_flat_source_column_matches_table(name):
    if not FLAT[name].exists():
        pytest.skip("not generated")
    values = set(pq.read_table(FLAT[name], columns=["source"]).column("source").to_pylist())
    assert values == {name}


@pytest.mark.parametrize("name", SOURCES)
def test_raw_json_is_valid_json(name):
    if not FLAT[name].exists():
        pytest.skip("not generated")
    raw = pq.read_table(FLAT[name], columns=["raw_json"]).column("raw_json").to_pylist()
    bad = []
    for i, v in enumerate(raw):
        try:
            json.loads(v)
        except (TypeError, ValueError):
            bad.append(i)
    assert not bad, f"{name}: raw_json invalid at rows {bad[:10]}"


def test_links_reference_existing_gudid_devices():
    if not (LINKS.exists() and FLAT["gudid"].exists()):
        pytest.skip("not generated")
    gudid = set(pq.read_table(FLAT["gudid"], columns=["udi_di"]).column("udi_di").to_pylist())
    linked = set(pq.read_table(LINKS, columns=["udi_di"]).column("udi_di").to_pylist())
    orphans = linked - gudid
    assert not orphans, f"links reference udi_di not in gudid: {sorted(orphans)[:5]}"


def test_links_reference_existing_maude_records():
    if not (LINKS.exists() and FLAT["maude"].exists()):
        pytest.skip("not generated")
    maude = set(pq.read_table(FLAT["maude"], columns=["source_record_id"])
                .column("source_record_id").to_pylist())
    linked = set(pq.read_table(LINKS, columns=["source_record_id"])
                 .column("source_record_id").to_pylist())
    orphans = linked - maude
    assert not orphans, f"links reference maude ids not in maude: {sorted(orphans)[:5]}"


def test_links_count_is_reasonable():
    """Exact linking on the sample data should produce a small number of links.

    A much larger count usually means the join fanned out (for example a
    blank or repeated udi_di matching many devices).
    """
    if not LINKS.exists():
        pytest.skip("not generated")
    n = pq.read_metadata(LINKS).num_rows
    assert n <= 500, f"Unexpectedly many links: {n}"
