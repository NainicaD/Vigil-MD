"""Tests for scripts/warehouse/schemas.py and create_tables.py --print-only.

Pure data checks. No GCP.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.warehouse import schemas

REPO = str(Path(__file__).resolve().parents[1])
SPECS = schemas.table_specs("p")


def columns(spec):
    return [n for n, _, _ in spec["columns"]]


def test_sixteen_tables():
    ids = [s["table_id"] for s in SPECS]
    assert len(ids) == 16 == len(set(ids))
    assert sum(".vigil_md_raw." in i for i in ids) == 4
    assert sum(".vigil_md_staging." in i for i in ids) == 5     # + maude_events_stg
    assert sum(".vigil_md_prod." in i for i in ids) == 6        # + maude_problems, maude_text
    assert "p.vigil_md_ops.load_log" in ids


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s["table_id"])
def test_cluster_and_partition_fields_are_columns(spec):
    cols = columns(spec)
    assert set(spec["cluster"]) <= set(cols)
    if spec["partition"]:
        assert spec["partition"][0] in cols


@pytest.mark.parametrize("source", schemas.SOURCES)
def test_merge_keys_are_staging_and_prod_columns(source):
    for table_id in (schemas.staging_table("p", source), schemas.prod_table("p", source)):
        spec = next(s for s in SPECS if s["table_id"] == table_id)
        assert set(schemas.MERGE_KEYS[source]) <= set(columns(spec))
        assert spec["cluster"] == schemas.MERGE_KEYS[source]


def test_raw_and_staging_require_a_partition_filter():
    for s in SPECS:
        if ".vigil_md_raw." in s["table_id"] or ".vigil_md_staging." in s["table_id"]:
            assert s["partition"] == ("_ingest_date", "DAY") and s["require_partition_filter"]


def test_prod_partitioning():
    for source in schemas.SOURCES:
        spec = next(s for s in SPECS if s["table_id"] == schemas.prod_table("p", source))
        if source in schemas.DATE_PARTITIONED_PROD:
            assert spec["partition"] == ("_part_date", "MONTH") and "_part_date" in columns(spec)
        else:
            assert spec["partition"] is None and "_part_date" not in columns(spec)


def test_gudid_merge_key_is_public_device_record_key():
    assert schemas.MERGE_KEYS["gudid"] == ["source_record_id"]


@pytest.mark.parametrize("table_id", ["p.vigil_md_processed.maude_flat", "p.vigil_md_raw.maude_sample"])
def test_protected_tables_are_refused(table_id):
    with pytest.raises(ValueError, match="protected"):
        schemas.assert_not_protected(table_id)


def test_create_tables_print_only_makes_no_gcp_call():
    code = ("import runpy, sys; sys.argv=['c','--project','p','--print-only'];"
            "runpy.run_path('scripts/warehouse/create_tables.py', run_name='__main__');"
            "print('GCP', any(m.startswith('google.cloud') for m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "=== Tables (16) ===" in r.stdout and "GCP False" in r.stdout
    assert "[partitions expire after 14 days]" in r.stdout


# ── MAUDE event tables ────────────────────────────────────────────────────────

def spec_of(table_id):
    return next(s for s in SPECS if s["table_id"] == table_id)


def test_maude_events_staging_expires_and_needs_a_partition_filter():
    spec = spec_of(schemas.maude_events_staging_table("p"))
    assert spec["partition"] == ("_ingest_date", "DAY") and spec["require_partition_filter"]
    assert spec["partition_expiration_days"] == 14 == schemas.MAUDE_EVENTS_STAGING_EXPIRY_DAYS
    assert spec["cluster"] == ["mdr_report_key"]
    modes = {n: m for n, _, m in spec["columns"]}
    assert modes["product_problems"] == modes["patient_problems"] == "REPEATED"
    assert modes["mdr_report_key"] == "REQUIRED"


@pytest.mark.parametrize("name", ["maude_problems", "maude_text"])
def test_maude_event_prod_tables(name):
    spec = spec_of(schemas.maude_event_prod_table("p", name))
    assert spec["partition"] == ("_part_date", "MONTH")      # same as prod maude
    assert spec["cluster"] == ["mdr_report_key"]
    assert "partition_expiration_days" not in spec            # prod rows never expire
    assert set(schemas.MAUDE_EVENT_KEYS[name]) <= set(columns(spec))


def test_only_the_events_staging_table_expires():
    expiring = [s["table_id"] for s in SPECS if s.get("partition_expiration_days")]
    assert expiring == [schemas.maude_events_staging_table("p")]


def test_unknown_maude_event_table_is_refused():
    with pytest.raises(ValueError):
        schemas.maude_event_prod_table("p", "maude")
