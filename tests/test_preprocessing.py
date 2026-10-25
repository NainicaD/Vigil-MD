"""Unit tests for preprocessing normalization functions.

Tests manufacturer name normalization, model number comparison keys,
and date parsing. All tests run without network access or GCS.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preprocessing.flatten_to_parquet import (
    normalize_manufacturer,
    normalize_date,
)
from scripts.matching.blocking import model_key


# ── Manufacturer name normalization ───────────────────────────────────────────

class TestManufacturerNormalization:

    def test_strips_inc_suffix(self):
        assert normalize_manufacturer("Acme Medical Inc.") == "acme medical"

    def test_strips_incorporated(self):
        assert normalize_manufacturer("ACME MEDICAL INCORPORATED") == "acme medical"

    def test_strips_corp(self):
        assert normalize_manufacturer("Baxter Healthcare Corp.") == "baxter healthcare"

    def test_strips_corporation(self):
        assert normalize_manufacturer("BAXTER HEALTHCARE CORPORATION") == "baxter healthcare"

    def test_strips_llc(self):
        assert normalize_manufacturer("Zyno Medical LLC") == "zyno medical"

    def test_strips_ltd(self):
        assert normalize_manufacturer("Smith Medical Ltd.") == "smith medical"

    def test_lowercases(self):
        assert normalize_manufacturer("MEDTRONIC") == "medtronic"

    def test_collapses_whitespace(self):
        assert normalize_manufacturer("Medtronic  MiniMed") == "medtronic minimed"

    def test_same_company_different_formats(self):
        """Core entity resolution test — these must produce the same key."""
        a = normalize_manufacturer("Baxter Healthcare Corp.")
        b = normalize_manufacturer("BAXTER HEALTHCARE CORPORATION")
        c = normalize_manufacturer("Baxter Healthcare")
        assert a == b == c

    def test_empty_string_returns_none(self):
        assert normalize_manufacturer("") is None

    def test_none_returns_none(self):
        assert normalize_manufacturer(None) is None

    def test_whitespace_only_returns_none(self):
        assert normalize_manufacturer("   ") is None

    def test_preserves_meaningful_subsidiary_name(self):
        """Medtronic MiniMed should stay distinct from plain Medtronic."""
        a = normalize_manufacturer("MEDTRONIC MINIMED, INC.")
        b = normalize_manufacturer("Medtronic, Inc.")
        assert a != b
        assert "minimed" in a
        assert "minimed" not in b


# ── Model number comparison keys ──────────────────────────────────────────────

class TestModelKey:

    def test_hyphen_removed(self):
        """IP-200 and IP200 should match."""
        assert model_key("IP-200") == model_key("IP200")

    def test_space_removed(self):
        assert model_key("IP 200") == model_key("IP200")

    def test_case_insensitive(self):
        assert model_key("mmt-332a") == model_key("MMT-332A")

    def test_different_models_stay_distinct(self):
        """IP-200 and IP-200S must NOT match."""
        assert model_key("IP-200") != model_key("IP-200S")

    def test_none_returns_none(self):
        assert model_key(None) is None

    def test_empty_returns_none(self):
        assert model_key("") is None


# ── Date normalization ────────────────────────────────────────────────────────

class TestNormalizeDate:

    def test_yyyymmdd_format(self):
        assert normalize_date("20200427") == "2020-04-27"

    def test_mm_dd_yyyy_format(self):
        assert normalize_date("04/27/2020") == "2020-04-27"

    def test_already_iso_format(self):
        assert normalize_date("2020-04-27") == "2020-04-27"

    def test_none_returns_none(self):
        assert normalize_date(None) is None

    def test_empty_returns_none(self):
        assert normalize_date("") is None

    def test_invalid_format_returns_none(self):
        assert normalize_date("not-a-date") is None