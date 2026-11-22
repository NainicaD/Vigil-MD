"""s13: candidate SQL (copies of the test-set rules), the write guard, and main with a fake BigQuery."""
import re
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from scripts import s13_build_candidates as s13

ROOT = Path(__file__).resolve().parents[1]
TEST_SET_SQL = ROOT / "sql" / "test_set" / "02_candidates.sql"
SHARED_CTES = ["s_ids", "g_raw", "g", "g2", "g_ids", "id_exact", "same_mfr_ids", "id_contains",
               "id_fuzzy", "name_same_mfr", "name_any_mfr", "pairs", "model_groups"]
S12_REQUIRED = ["maude_id", "gudid_id", "meta_n_dis_for_model",
                "key_m_mfr", "key_m_name", "key_m_model_raw", "key_m_catalog_raw", "key_m_pc",
                "key_g_mfr", "key_g_name", "key_g_model_raw", "key_g_catalog_raw", "key_g_pcs"]


def ctes(sql: str) -> dict[str, str]:
    """Top-level CTE name -> body text."""
    return {m.group(1): m.group(2) for m in re.finditer(r"^(\w+) AS \(\n(.*?)^\)", sql, re.S | re.M)}


# ── SQL ───────────────────────────────────────────────────────────────────────

def test_shared_ctes_are_exact_copies_of_the_test_set_rules():
    ours, theirs = ctes(s13.SQL_FILE.read_text()), ctes(TEST_SET_SQL.read_text())
    for name in SHARED_CTES:
        assert ours[name] == theirs[name], f"{name} differs from sql/test_set/02_candidates.sql"


def test_random_control_candidates_are_not_used():
    sql = s13.select_sql("p", "vigil_md_dev")
    assert "random_same_mfr" not in sql and "@cap_random" not in sql
    assert "cap_random" not in s13.CAPS


def test_output_has_every_column_s12_needs():
    final = s13.select_sql("p", "vigil_md_dev").rsplit("\nSELECT\n", 1)[1].split("\nFROM keyed")[0]
    aliases = re.findall(r"\bAS (\w+),?$", final, re.M)
    assert set(S12_REQUIRED) <= set(aliases)


def test_reads_only_its_dataset_and_writes_only_vigil_md_model():
    sql = s13.create_sql("p", "vigil_md_dev")
    assert sql.startswith("CREATE OR REPLACE TABLE `p.vigil_md_model.match_candidates_vigil_md_dev` AS\nWITH")
    read = set(re.findall(r"`([^`]+)`", sql.split(" AS\n", 1)[1]))
    assert read == {"p.vigil_md_dev.maude", "p.vigil_md_dev.gudid"}


def test_only_prod_or_a_dev_extract_can_be_read():
    assert "p.vigil_md_prod.maude" in s13.select_sql("p", "vigil_md_prod")
    for ds in ("vigil_md_raw", "vigil_md_staging", "vigil_md_model"):
        with pytest.raises(ValueError):
            s13.select_sql("p", ds)
    assert s13.main(["--project", "p", "--dataset", "vigil_md_raw", "--print-only"]) == s13.EXIT_BAD


def test_since_filters_the_pool():
    assert "WHERE (@since IS NULL OR t.last_updated_at > @since)" in s13.select_sql("p", "vigil_md_dev")


def test_mode_picks_the_pool_or_a_sample_of_udi_linked_rows():
    sql = s13.select_sql("p", "vigil_md_dev")
    assert "IF(@mode = 'udi_check',\n           d.udi_di IS NOT NULL" in sql and "< @sample_per_10k" in sql
    assert "d.udi_di IS NULL)" in sql                                   # 'pool': no usable UDI link
    final = sql.rsplit("\nSELECT\n", 1)[1]
    assert "AS cand_group_key" in final and "AS true_group_key" in final


def test_udi_check_never_replaces_the_pools_table():
    assert s13.target_table("p", "vigil_md_dev", "udi_check") == "p.vigil_md_model.match_candidates_check_vigil_md_dev"
    assert s13.create_sql("p", "vigil_md_dev", "udi_check").startswith(
        "CREATE OR REPLACE TABLE `p.vigil_md_model.match_candidates_check_vigil_md_dev`")
    assert "match_candidates_check_vigil_md_dev" in s13.siblings_sql("p", "vigil_md_dev", "udi_check")


def test_udi_check_run_scores_the_whole_sample(bq, tmp_path):
    client = FakeClient(rows=[ROW], last="2026-11-20T06:00:00+00:00")
    assert s13.main(["--project", "p", "--mode", "udi_check", "--sample-per-10k", "50",
                     "--out", str(tmp_path / "c.parquet")], client=client) == 0
    (sql, cfg), = client.ran
    params = {n: v for n, _, v in cfg.query_parameters}
    assert params["since"] is None and params["mode"] == "udi_check" and params["sample_per_10k"] == 50
    assert "match_candidates_check_vigil_md_dev" in sql


# ── main (fake BigQuery) ──────────────────────────────────────────────────────

class NotFound(Exception):
    pass


class FakeClient:
    def __init__(self, rows=None, dry_gb=1.0, last=None, link_table=True, sib_dry_gb=0.2):
        self.rows = pd.DataFrame(rows or [])
        self.dry_gb, self.last, self.link_table, self.sib_dry_gb = dry_gb, last, link_table, sib_dry_gb
        self.ran, self.dry, self.sib = [], [], []

    def query(self, sql, job_config=None):
        if sql.startswith("SELECT MAX(built_at)"):
            if not self.link_table:
                raise NotFound(sql)
            return types.SimpleNamespace(result=lambda: [{"t": self.last}])
        if "gkeys" in sql:                                   # the sibling-key query
            if job_config.dry_run is True:
                return types.SimpleNamespace(total_bytes_processed=int(self.sib_dry_gb * 1e9))
            self.sib.append((sql, job_config))
            return types.SimpleNamespace(result=lambda: [{"mfr": "medtronic", "sib_key": "A1"}],
                                         total_bytes_processed=int(self.sib_dry_gb * 1e9))
        if job_config.dry_run is True:
            self.dry.append(job_config)
            return types.SimpleNamespace(total_bytes_processed=int(self.dry_gb * 1e9))
        self.ran.append((sql, job_config))
        return types.SimpleNamespace(result=lambda: None, total_bytes_billed=int(self.dry_gb * 1e9))

    def list_rows(self, table):
        return types.SimpleNamespace(to_dataframe=lambda create_bqstorage_client: self.rows)


@pytest.fixture
def bq(fake_bigquery, monkeypatch):
    """QueryJobConfig keeps its kwargs; NotFound comes from a fake google.api_core."""
    fake_bigquery.QueryJobConfig = lambda **kw: types.SimpleNamespace(**{"dry_run": False, **kw})
    fake_bigquery.ScalarQueryParameter = lambda name, typ, value: (name, typ, value)
    exc = types.ModuleType("google.api_core.exceptions")
    exc.NotFound = NotFound
    monkeypatch.setitem(sys.modules, "google.api_core", types.ModuleType("google.api_core"))
    monkeypatch.setitem(sys.modules, "google.api_core.exceptions", exc)
    return fake_bigquery


def since_of(job_config):
    return dict((n, v) for n, _, v in job_config.query_parameters)["since"]


ROW = {"maude_id": "m1|1", "gudid_id": "g1", "meta_n_pool_rows": 5}


def test_print_only_makes_no_gcp_call(capsys):
    client = FakeClient()
    assert s13.main(["--project", "p", "--print-only"], client=client) == 0
    assert client.ran == [] and client.dry == []
    assert capsys.readouterr().out.startswith("CREATE OR REPLACE TABLE `p.vigil_md_model.")


def test_full_run_writes_the_parquet_and_caps_billing(bq, tmp_path, capsys):
    client, out = FakeClient(rows=[ROW, {**ROW, "gudid_id": "g2"}]), tmp_path / "c.parquet"
    assert s13.main(["--project", "p", "--full", "--out", str(out), "--max-gb", "5"], client=client) == 0
    (sql, cfg), = client.ran
    assert sql.startswith("CREATE OR REPLACE TABLE") and cfg.maximum_bytes_billed == int(5e9)
    assert since_of(cfg) is None
    assert len(pd.read_parquet(out)) == 2
    assert "2 pairs for 1 MAUDE rows (pool: 5 rows)" in capsys.readouterr().out


def test_since_auto_uses_the_last_load_or_everything(bq, tmp_path):
    out = tmp_path / "c.parquet"
    client = FakeClient(rows=[ROW], last="2026-11-20T06:00:00+00:00")
    s13.main(["--project", "p", "--out", str(out)], client=client)
    assert since_of(client.ran[0][1]) == "2026-11-20T06:00:00+00:00"
    client = FakeClient(rows=[ROW], link_table=False)          # never loaded: every row
    s13.main(["--project", "p", "--out", str(out)], client=client)
    assert since_of(client.ran[0][1]) is None


def test_no_candidates_exits_4_and_writes_nothing(bq, tmp_path):
    out = tmp_path / "c.parquet"
    assert s13.main(["--project", "p", "--full", "--out", str(out)], client=FakeClient()) == s13.EXIT_NONE
    assert not out.exists()


def test_sibling_query_reads_this_runs_candidates_and_gudid():
    sql = s13.siblings_sql("p", "vigil_md_dev")
    tables = set(re.findall(r"`([^`]+)`", sql))
    assert tables == {"p.vigil_md_dev.gudid", "p.vigil_md_model.match_candidates_vigil_md_dev"}
    for col in ("key_m_model_raw", "key_m_catalog_raw", "key_g_mfr"):
        assert col in sql                                    # columns the candidate table has
    assert "{{" not in sql                                   # usable_key macros expanded


def test_print_only_shows_both_queries(capsys):
    assert s13.main(["--project", "p", "--print-only"], client=FakeClient()) == 0
    out = capsys.readouterr().out
    assert "CREATE OR REPLACE TABLE" in out and "-- sibling keys" in out and "gkeys" in out


def test_run_writes_the_sibling_keys_for_s12(bq, tmp_path, capsys):
    from scripts.s04_build_feature_table import read_siblings
    client, out = FakeClient(rows=[ROW]), tmp_path / "c.parquet"
    assert s13.main(["--project", "p", "--full", "--out", str(out), "--max-gb", "5"], client=client) == 0
    (_, cfg), = client.sib
    assert cfg.maximum_bytes_billed == int(5e9)
    assert read_siblings(tmp_path / "c.siblings.csv") == {"medtronic": frozenset({"A1"})}
    other = tmp_path / "s.csv"
    s13.main(["--project", "p", "--full", "--out", str(out), "--siblings-out", str(other)], client=client)
    assert other.exists()


def test_sibling_query_over_budget_is_not_run(bq, tmp_path):
    client = FakeClient(rows=[ROW], sib_dry_gb=20)
    assert s13.main(["--project", "p", "--full", "--out", str(tmp_path / "c.parquet"),
                     "--max-gb", "10"], client=client) == s13.EXIT_BAD
    assert client.sib == []


def test_over_budget_runs_nothing(bq, tmp_path):
    client = FakeClient(rows=[ROW], dry_gb=20)
    assert s13.main(["--project", "p", "--full", "--out", str(tmp_path / "c.parquet"),
                     "--max-gb", "10"], client=client) == s13.EXIT_BAD
    assert client.ran == []
