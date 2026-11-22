"""Tests for scripts/warehouse/report_delta.py and its use in unzip / load (changed reports only).

GCS is the in-memory FakeBucket. No network."""
import gzip
import json

import pytest

from scripts.warehouse import common
from scripts.warehouse import report_delta as rd
from scripts.warehouse import unzip_to_ndjson as u
from tests.conftest import FakeBucket

IGN = rd.DELTA_SOURCES["maude"]["ignore"]


def report(key, udi="0001", problems=("Burn", "Burn"), seq="1", text_order=(1, 2), **extra):
    texts = [{"mdr_text_key": str(i), "text": f"t{i}"} for i in text_order]
    return {"mdr_report_key": key, "device": [{"udi_di": udi, "device_sequence_number": seq}],
            "patient": [{"patient_problems": list(problems)}], "mdr_text": texts, **extra}


# ── fingerprint ──────────────────────────────────────────────────────────────

def test_fingerprint_ignores_order_scalar_duplicates_and_the_renumbered_counter():
    a = report("1", problems=("Burn", "Burn"), seq="11", text_order=(1, 2))
    b = report("1", problems=("Burn",), seq="99", text_order=(2, 1))
    assert rd.fingerprint(a, IGN) == rd.fingerprint(b, IGN)


@pytest.mark.parametrize("change", [
    {"udi": ""},                                    # FDA blanks the UDI-DI (2026-11-17)
    {"problems": ("Burn", "Fibrosis")},
    {"reporter_country_code": ""},                  # a field no table reads still counts
])
def test_any_other_change_changes_the_fingerprint(change):
    assert rd.fingerprint(report("1"), IGN) != rd.fingerprint(report("1", **change), IGN)


def test_identical_objects_in_a_list_are_not_merged():
    one = {"mdr_report_key": "1", "device": [{"udi_di": "A"}]}
    two = {"mdr_report_key": "1", "device": [{"udi_di": "A"}, {"udi_di": "A"}]}
    assert rd.fingerprint(one) != rd.fingerprint(two)


def test_newest_promoted_shard_wins_and_other_versions_are_ignored():
    assert rd.merge_shards([(2, {"r": "new"}), (1, {"r": "old", "s": "x"})]) == {"r": "new", "s": "x"}
    other = gzip.compress(json.dumps({"fp_version": rd.FP_VERSION + 1, "fingerprints": {}}).encode())
    assert rd.decode_shard(other) is None
    assert rd.decode_shard(rd.encode_shard({"r": "f"})) == {"r": "f"}


def test_report_delta_counts_new_changed_unchanged():
    d = rd.ReportDelta("maude", {"1": rd.fingerprint(report("1"), IGN), "2": "stale"})
    assert [d.keep(report(k)) for k in ("1", "2", "3")] == [False, True, True]
    assert d.summary() == {"fp_version": rd.FP_VERSION, "records": 3, "written": 2, "new": 1,
                           "changed": 1, "unchanged": 1}
    with pytest.raises(RuntimeError, match="mdr_report_key"):
        d.keep({"device": []})


# ── unzip + load with the fingerprint store ──────────────────────────────────

D1, D2 = "2026-11-03", "2026-11-17"
KEY = "2026q1/device-event-0001-of-0001.json.zip"


def run_unzip(bucket, make_openfda_zip, records, ingest_date, state, scope="changed"):
    path = f"raw/event/{ingest_date}/{KEY}"
    bucket.store[path] = make_openfda_zip(records)
    entry = {"source": "maude", "partition_key": KEY, "gcs_path": path}
    lookups = u.QuarterLookups(bucket, "maude", use_store=(scope == "changed"))
    must_load = u.process_entry(bucket, "maude", ingest_date, entry, state, lookups)
    nd = gzip.decompress(bucket.store[common.ndjson_path("maude", ingest_date, KEY)]).decode()
    return must_load, entry, [json.loads(l)["payload"]["mdr_report_key"] for l in nd.splitlines()]


def committed(bucket, ingest_date, entry, state):
    """What load_raw_to_bq does after a commit: partition state, then promote."""
    state[KEY] = {"content_sha256": entry["content_sha256"]}
    assert rd.promote(bucket, "maude", ingest_date, KEY)


def test_first_run_loads_everything_then_a_reshuffle_loads_nothing(make_openfda_zip):
    bucket, state = FakeBucket(), {}
    first = [report("1"), report("2"), report("3")]
    must, entry, written = run_unzip(bucket, make_openfda_zip, first, D1, state)
    assert must and written == ["1", "2", "3"] and entry["row_count"] == 3
    assert not bucket.store.get(rd.current_path("maude", KEY))       # not before the commit
    committed(bucket, D1, entry, state)

    # same reports, new order, renumbered counter, duplicates removed: new bytes, nothing new
    again = [report("3", seq="7"), report("1", problems=("Burn",)), report("2", text_order=(2, 1))]
    must, entry, written = run_unzip(bucket, make_openfda_zip, again, D2, state)
    assert must is False and entry["skip_load"] is True and written == []
    assert entry["delta"]["unchanged"] == 3
    assert rd.pending_path("maude", D2, KEY) not in bucket.store      # promoted at unzip


def test_a_changed_and_a_new_report_are_the_only_rows_loaded(make_openfda_zip):
    bucket, state = FakeBucket(), {}
    _, entry, _ = run_unzip(bucket, make_openfda_zip, [report("1"), report("2")], D1, state)
    committed(bucket, D1, entry, state)
    must, entry, written = run_unzip(bucket, make_openfda_zip,
                                     [report("1"), report("2", udi=""), report("4")], D2, state)
    assert must and written == ["2", "4"] and entry["row_count"] == 2
    assert entry["delta"] == {"fp_version": rd.FP_VERSION, "records": 3, "written": 2,
                              "new": 1, "changed": 1, "unchanged": 1}


def test_a_failed_load_does_not_mark_reports_as_seen(make_openfda_zip):
    bucket, state = FakeBucket(), {}
    _, entry, _ = run_unzip(bucket, make_openfda_zip, [report("1")], D1, state)
    committed(bucket, D1, entry, state)
    run_unzip(bucket, make_openfda_zip, [report("1", udi="")], D2, state)   # load fails: no promote
    _, _, written = run_unzip(bucket, make_openfda_zip, [report("1", udi="")], D2, state)
    assert written == ["1"]                                     # the retry still loads it


def test_full_load_writes_every_report_but_still_saves_fingerprints(make_openfda_zip):
    bucket, state = FakeBucket(), {}
    _, entry, _ = run_unzip(bucket, make_openfda_zip, [report("1")], D1, state)
    committed(bucket, D1, entry, state)
    must, entry, written = run_unzip(bucket, make_openfda_zip, [report("1"), report("2")], D2,
                                     {}, scope="all")
    assert must and written == ["1", "2"]
    assert rd.pending_path("maude", D2, KEY) in bucket.store


def test_other_sources_are_not_fingerprinted(make_openfda_zip):
    bucket = FakeBucket()
    path = "raw/enforcement/2026-10-20/enforcement/device-enforcement-0001-of-0001.json.zip"
    bucket.store[path] = make_openfda_zip([{"recall_number": "Z-1"}])
    entry = {"source": "enforcement", "partition_key": "enforcement/x.json.zip", "gcs_path": path}
    u.process_entry(bucket, "enforcement", D1, entry, {}, u.QuarterLookups(bucket, "enforcement"))
    assert "delta" not in entry and not any(n.startswith(rd.PREFIX) for n in bucket.store)


def test_promote_is_a_no_op_without_a_pending_shard():
    assert rd.promote(FakeBucket(), "maude", D1, KEY) is False
