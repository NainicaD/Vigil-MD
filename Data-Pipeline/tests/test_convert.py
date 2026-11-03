"""Tests for scripts/warehouse/convert.py: zip → {"payload": record} NDJSON.

Pure code: in-memory zips, no GCP, no network.
"""
import io
import json
import xml.etree.ElementTree as ET
import zipfile

import pytest

from scripts.warehouse import convert


def lines_of(out: io.StringIO) -> list[dict]:
    return [json.loads(line)["payload"] for line in out.getvalue().splitlines()]


def run(zip_bytes: bytes, source: str):
    out = io.StringIO()
    rows, sha = convert.convert_zip(io.BytesIO(zip_bytes), out, source)
    return rows, sha, lines_of(out)


# ── openFDA ──────────────────────────────────────────────────────────────────

RECORDS = [{"recall_number": "Z-1-2024", "rate": 1.5, "nested": {"x": [1, 2]}},
           {"recall_number": "Z-2-2024", "rate": None, "text": "ünïcode\nline"}]


def test_openfda_payload_equals_source_records(make_openfda_zip):
    rows, _, payloads = run(make_openfda_zip(RECORDS), "enforcement")
    assert rows == 2
    assert payloads == RECORDS


def test_openfda_floats_stay_floats(make_openfda_zip):
    # without ijson use_float=True the value is a Decimal and json.dumps fails
    _, _, payloads = run(make_openfda_zip(RECORDS), "maude")
    assert isinstance(payloads[0]["rate"], float)


def test_openfda_zero_records_raises(make_openfda_zip):
    with pytest.raises(RuntimeError, match="0 records"):
        run(make_openfda_zip([]), "recall")


@pytest.mark.parametrize("members", [
    {"readme.txt": "x"},
    {"a.json": '{"results": []}', "b.json": '{"results": []}'},
])
def test_openfda_needs_exactly_one_json_member(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    with pytest.raises(RuntimeError, match="exactly 1 .json member"):
        run(buf.getvalue(), "maude")


# ── content hash ─────────────────────────────────────────────────────────────

def test_hash_is_stable_across_meta_key_order_and_member_name(make_openfda_zip):
    _, h1, _ = run(make_openfda_zip(RECORDS), "enforcement")
    reordered = [dict(reversed(list(r.items()))) for r in RECORDS]
    _, h2, _ = run(make_openfda_zip(reordered, member="other.json",
                                    meta={"last_updated": "2026-11-01"}), "enforcement")
    assert h1 == h2


def test_hash_changes_when_one_record_changes(make_openfda_zip):
    _, h1, _ = run(make_openfda_zip(RECORDS), "enforcement")
    _, h2, _ = run(make_openfda_zip([RECORDS[0], dict(RECORDS[1], rate=2.0)]), "enforcement")
    assert h1 != h2


# ── GUDID XML → dict ─────────────────────────────────────────────────────────

def test_gudid_device_fields(make_gudid_zip, gudid_device):
    _, _, (d,) = run(make_gudid_zip([[gudid_device("k1", device_id="ZBAC1401",
                                                    brand="Dongle ")]]), "gudid")
    assert d["publicDeviceRecordKey"] == "k1"
    assert d["brandName"] == "Dongle "                      # text kept exactly
    assert d["deviceCommDistributionEndDate"] is None      # xsi:nil
    assert d["contacts"] is None                           # empty element
    assert not any(k.startswith("{") for k in d)           # namespace stripped


def test_gudid_plural_wrappers_are_always_lists(make_gudid_zip, gudid_device):
    _, _, (d,) = run(make_gudid_zip([[gudid_device("k1", product_codes=("NOF",))]]), "gudid")
    assert d["identifiers"] == {"identifier": [
        {"deviceId": "D1", "deviceIdType": "Primary", "pkgQuantity": None}]}
    assert d["productCodes"] == {"fdaProductCode": [{"productCode": "NOF", "productCodeName": "n"}]}


def test_xml_to_value_repeated_tags_and_attributes():
    elem = ET.fromstring(
        '<sterilization xmlns="http://www.fda.gov/cdrh/gudid">'
        '<method>EO</method><method>Gamma</method>'
        '<size unit="cm">5</size><deviceSterile>false</deviceSterile></sterilization>')
    assert convert.xml_to_value(elem) == {
        "method": ["EO", "Gamma"],
        "size": {"@unit": "cm", "#text": "5"},
        "deviceSterile": "false",
    }


def test_gudid_parts_are_read_in_part_order(make_gudid_zip, gudid_device):
    buf = io.BytesIO()
    parts = zipfile.ZipFile(io.BytesIO(make_gudid_zip(
        [[gudid_device("p1")], [gudid_device("p2")], [gudid_device("p3")]])))
    with zipfile.ZipFile(buf, "w") as z:                    # write members in reverse
        for name in reversed(sorted(parts.namelist())):
            z.writestr(name, parts.read(name))
    _, _, payloads = run(buf.getvalue(), "gudid")
    assert [p["publicDeviceRecordKey"] for p in payloads] == ["p1", "p2", "p3"]


def test_gudid_parts_rejects_missing_part():
    with pytest.raises(RuntimeError, match=r"missing \[2\]"):
        convert.gudid_parts(["FULLDownload_Part1_Of_3_2026-10-01.xml",
                             "FULLDownload_Part3_Of_3_2026-10-01.xml"])


def test_gudid_parts_rejects_mixed_totals():
    with pytest.raises(RuntimeError, match="one 'Of_N' total"):
        convert.gudid_parts(["FULLDownload_Part1_Of_2_2026-10-01.xml",
                             "FULLDownload_Part2_Of_3_2026-10-01.xml"])


def test_gudid_part_count_must_match_header(make_gudid_zip, gudid_device):
    with pytest.raises(RuntimeError, match="part has 1 devices, header says 2"):
        run(make_gudid_zip([[gudid_device("k1")]], per_part=2, db_total=1), "gudid")


def test_gudid_total_must_match_header(make_gudid_zip, gudid_device):
    with pytest.raises(RuntimeError, match="numberRecordsDatabase says 9"):
        run(make_gudid_zip([[gudid_device("k1")]], db_total=9), "gudid")


def test_gudid_hash_is_stable_across_new_part_names(make_gudid_zip, gudid_device):
    devices = [[gudid_device("k1"), gudid_device("k2")]]
    _, h1, _ = run(make_gudid_zip(devices, date="2026-10-01"), "gudid")
    _, h2, _ = run(make_gudid_zip(devices, date="2026-11-01"), "gudid")
    assert h1 == h2


# ── Utf8Writer ───────────────────────────────────────────────────────────────

def test_utf8_writer_encodes_and_never_closes():
    raw = io.BytesIO()
    w = convert.Utf8Writer(raw)
    w.write("ü\n")
    assert raw.getvalue() == "ü\n".encode("utf-8")
    # no close(): the binary BlobWriter's own __exit__ decides finalize vs terminate
    assert not hasattr(w, "close")
