"""Unit tests for preprocessing normalization functions.

Tests manufacturer name normalization, model number comparison keys,
and date parsing. All tests run without network access or GCS.
"""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preprocessing.flatten_to_parquet import (
    clean_text,
    maude_problems,
    maude_text,
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

    def test_strips_llc_with_period(self):
        assert normalize_manufacturer("Foo, LLC.") == "foo"

    @pytest.mark.parametrize("name", ["Tesco", "Costco", "Medico", "Carco",
                                      "US Surgical Puerto Rico", "Medtronic Mexico"])
    def test_keeps_names_that_end_in_suffix_letters(self, name):
        assert normalize_manufacturer(name) == name.lower()

    def test_strips_suffix_without_space_after_comma(self):
        assert normalize_manufacturer("Acme,Inc") == "acme"

    @pytest.mark.parametrize("name, expected", [
        ("Gentec(Shanghai)Corporation", "gentec(shanghai)"),
        ("J.Morita Mfg.Corp", "j.morita mfg"),
        ("Mediana.Co.Ltd", "mediana.co"),
        ("Medical Wire & Equipment Co.(Bath)Limited", "medical wire & equipment co.(bath)"),
    ])
    def test_strips_suffix_after_punctuation(self, name, expected):
        assert normalize_manufacturer(name) == expected

    @pytest.mark.parametrize("name", ["AcumedLLC", "AcumedLLC.", "Acumed LLC", "Acumed, LLC."])
    def test_strips_glued_llc(self, name):
        assert normalize_manufacturer(name) == "acumed"

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


# ── MAUDE event tables ────────────────────────────────────────────────────────

def report(key="1", event_type="Injury", received="20240105", **kw):
    return {"mdr_report_key": key, "event_type": event_type, "date_received": received,
            "date_report": "20240101", **kw}


DESC = "Description of Event or Problem"
MFR = "Additional Manufacturer Narrative"
ALL = {"text_types": None, "event_types": None, "from_year": 2016, "to_year": 2026}


class TestMaudeProblems:

    def test_device_and_patient_terms_distinct_and_trimmed(self):
        r = report(product_problems=["Break", " Break ", "", "Leak"],
                   patient=[{"patient_problems": ["Pain", "Pain"]},
                            {"patient_problems": ["Pain", "Burn"]}])
        rows = maude_problems([r]).to_dict("records")
        got = [(x["problem_type"], x["problem_term"]) for x in rows]
        assert got == [("device", "Break"), ("device", "Leak"),
                       ("patient", "Burn"), ("patient", "Pain")]
        assert rows[0]["date_received"] == "2024-01-05" and rows[0]["event_type"] == "Injury"

    def test_reports_without_terms_or_key_give_no_rows(self):
        df = maude_problems([report(product_problems=None), report(key="", product_problems=["X"])])
        assert df.empty and list(df.columns)[:3] == ["mdr_report_key", "problem_type", "problem_term"]


class TestMaudeText:

    def test_default_filter_keeps_event_descriptions_of_death_and_injury(self):
        texts = [{"mdr_text_key": "a", "text_type_code": DESC, "text": "It broke."},
                 {"mdr_text_key": "b", "text_type_code": MFR, "text": "We checked."}]
        recs = [report("1", "Injury", mdr_text=texts),
                report("2", "Malfunction", mdr_text=texts),
                report("3", "Death", received="20150101", mdr_text=texts)]    # before 2016
        df = maude_text(recs)
        assert df[["mdr_report_key", "mdr_text_key"]].values.tolist() == [["1", "a"]]

    def test_empty_text_dropped_and_duplicate_keys_keep_the_longest(self):
        texts = [{"mdr_text_key": "a", "text_type_code": DESC, "text": "short"},
                 {"mdr_text_key": "a", "text_type_code": DESC, "text": "a longer text"},
                 {"mdr_text_key": "b", "text_type_code": DESC, "text": "   "}]
        df = maude_text([report(mdr_text=texts)], ALL)
        assert df["text"].tolist() == ["a longer text"]

    def test_no_filter_keeps_every_type(self):
        texts = [{"mdr_text_key": "a", "text_type_code": DESC, "text": "x"},
                 {"mdr_text_key": "b", "text_type_code": MFR, "text": "y"}]
        assert len(maude_text([report(event_type="Malfunction", mdr_text=texts)], ALL)) == 2


class TestCleanText:

    def test_control_characters_removed_tab_and_newline_kept(self):
        assert clean_text(" a\x00b\tc\nd ") == "ab\tc\nd"

    def test_invalid_utf8_replaced(self):
        assert clean_text("bad \ud83d end") == "bad ? end"

    @pytest.mark.parametrize("value", [None, "", "   ", 5])
    def test_empty_or_not_text_is_none(self, value):
        assert clean_text(value) is None
