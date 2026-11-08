"""Golden fixture (H4), offline. The BigQuery half is `build_labels.py --fixture`, run by hand; here it is mocked."""
import hashlib
from types import SimpleNamespace

import pytest

from pipeline import s01_build_label_set as bl
from vigil_model import golden_fixture as gf
from vigil_model import same_device_features as f

FILE_HASH, ROWS = gf.load()
BY_ID = {r["case_id"]: r for r in ROWS}

# Truths written by hand from docs/SAME_DEVICE_DEFINITION.md, NOT copied from the file. --regen cannot bless
# a wrong value for these cases without this test failing.
HAND_KINDS = {
    "c01": "same_model_key", "c03": "maude_model_eq_gudid_catalog", "c04": "maude_catalog_eq_gudid_model",
    "c05": "catalog_eq_catalog", "c06": "maude_model_missing_or_placeholder",
    "c07": "maude_model_missing_or_placeholder", "c08": "maude_model_missing_or_placeholder",
    "c09": "maude_model_missing_or_placeholder", "c10": "maude_model_missing_or_placeholder",
    "c11": "gudid_model_missing", "c12": "conflict", "c13": "prefix_related", "c14": "prefix_related",
    "c15": "contains", "c16": "conflict", "c17": "conflict", "c18": "same_model_key", "c19": "conflict", "c20": "conflict",
    "c24": "same_model_key", "c27": "gudid_model_missing", "c30": "conflict", "c31": "maude_model_missing_or_placeholder",
    "c32": "gudid_model_missing",
}


def test_file_has_the_cases_and_the_current_constants_hash():
    assert len(ROWS) >= 40 and len(BY_ID) == len(ROWS)
    assert FILE_HASH == f.constants_sha256(), "a rule constant changed: run golden_fixture.py --regen and review the diff"


def test_python_reproduces_every_expected_value():
    for r in ROWS:
        assert gf.python_outputs(r) == {c: r[c] for c in gf.OUTPUT_COLS}, r["case_id"]


@pytest.mark.parametrize("case,kind", HAND_KINDS.items())
def test_hand_checked_match_kinds(case, kind):
    assert BY_ID[case]["match_kind"] == kind


def test_hand_checked_keys_and_ids():
    assert BY_ID["c01"]["m_model_key"] == "GP200" and BY_ID["c24"]["m_model_key"] == "GP200AB"
    assert BY_ID["c31"]["m_model_key"] == "" and BY_ID["c09"]["m_model_key"] is None      # '' is not NULL
    assert BY_ID["c25"]["device_key"] == "|ABC123"                                         # NULL labeler -> ''
    assert BY_ID["c26"]["device_key"] == "acme|CAT9"                                       # catalog fallback
    assert BY_ID["c27"]["device_key"] is None and BY_ID["c27"]["split"] is None
    assert BY_ID["c28"]["pair_id"] == hashlib.sha256(b"1000028||00811111111128|udi_link").hexdigest()[:32]   # E11
    assert BY_ID["c29"]["pair_id"] != BY_ID["c28"]["pair_id"]
    assert {r["split"] for r in ROWS} == {None, "train", "test"}


def test_cell_syntax_round_trips():
    for value in (None, "", "GP 200", "a\tb", " x ", "café", "back\\slash"):
        assert gf.decode(gf.encode(value)) == value
    with pytest.raises(ValueError):
        gf.encode("\U0001f600")


def test_sql_has_no_leftovers_and_reads_no_table():
    sql = gf.fixture_sql(ROWS)
    assert "{{" not in sql and "__ROWS_JSON__" not in sql and "`" not in sql
    assert sql.lstrip().startswith("WITH") and "\\u000b" in sql      # the vertical tab reaches BigQuery as JSON


# --- BigQuery side, mocked ----------------------------------------------------------------------
class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self):
        return [dict(r, case_id=r["case_id"]) for r in self.rows]


class FakeClient:
    def __init__(self, rows):
        self.rows, self.sql, self.config = rows, None, None

    def query(self, sql, job_config=None):
        self.sql, self.config = sql, job_config
        return FakeJob(self.rows)


@pytest.fixture
def cfg(fake_bigquery):
    fake_bigquery.QueryJobConfig.side_effect = lambda **kw: SimpleNamespace(**kw)


def bq_rows():
    return [{"case_id": r["case_id"]} | {c: r[c] for c in gf.OUTPUT_COLS} for r in ROWS]


def test_matching_bigquery_output_passes_and_the_job_is_capped(cfg):
    client = FakeClient(bq_rows())
    assert gf.check_bigquery(client) == gf.EXIT_OK
    assert client.config.maximum_bytes_billed == int(1e8)
    assert "UPPER(REGEXP_REPLACE" in client.sql                  # the same macro text the label SQL uses


def test_a_difference_is_reported_with_exit_4(cfg, caplog):
    rows = bq_rows()
    rows[18]["m_model_key"] = "GP200"                            # BigQuery stripping the vertical tab
    assert gf.check_bigquery(FakeClient(rows)) == gf.EXIT_MISMATCH == 4
    assert "c19 m_model_key" in caplog.text


def test_a_missing_row_is_a_difference(cfg):
    assert gf.check_bigquery(FakeClient(bq_rows()[1:])) == gf.EXIT_MISMATCH


def test_stale_fixture_runs_no_query(cfg, tmp_path):
    stale = tmp_path / "g.csv"
    stale.write_text(gf.FIXTURE.read_text(encoding="utf-8").replace(gf.HASH_PREFIX + f.constants_sha256(),
                                                                      gf.HASH_PREFIX + "0" * 64), encoding="utf-8")
    client = FakeClient(bq_rows())
    assert gf.check_bigquery(client, stale) == gf.EXIT_STALE and client.sql is None


def test_fixture_flag_on_build_labels_calls_the_check(fake_bigquery, monkeypatch):
    monkeypatch.setattr(gf, "check_bigquery", lambda client: 7)
    assert bl.main(["--project", "p", "--fixture"]) == 7


def test_fixture_print_only_needs_no_gcp(capsys):
    assert bl.main(["--project", "p", "--fixture", "--print-only"]) == bl.EXIT_OK
    assert "JSON_QUERY_ARRAY" in capsys.readouterr().out
