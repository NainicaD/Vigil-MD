"""Tests for scripts/warehouse/backfill_last_known.py (issue #18). SQL as text; fake client."""
import types

from scripts.warehouse import backfill_last_known as B
from scripts.warehouse import schemas

PROD, STG = schemas.prod_table("p", "maude"), schemas.staging_table("p", "maude")


def test_update_restores_only_blank_rows_from_one_staging_partition_in_a_transaction():
    sql = B.render_update_sql("maude", PROD, STG, "udi_di")
    assert "WHERE _ingest_date = @from_date AND udi_di IS NOT NULL" in sql
    assert "T.source_record_id = S.source_record_id AND T.device_seq = S.device_seq" in sql
    assert "AND T.udi_di IS NULL" in sql
    assert "SET udi_di = S.udi_di, udi_di_removed_at = T.last_updated_at" in sql
    order = ["BEGIN TRANSACTION", "UPDATE", "IF @@row_count != @expected", "ROLLBACK", "RAISE",
             "COMMIT TRANSACTION"]
    assert [sql.index(s) for s in order] == sorted(sql.index(s) for s in order)


def test_add_column_is_idempotent():
    assert B.render_add_column_sql("maude", PROD) == [
        f"ALTER TABLE `{PROD}` ADD COLUMN IF NOT EXISTS udi_di_removed_at TIMESTAMP"]


def test_print_only_makes_no_gcp_call(capsys):
    assert B.main(["--project", "p", "--source", "maude", "--from-ingest-date", "2026-11-03",
                   "--print-only"]) == 0
    out = capsys.readouterr().out
    assert "ALTER TABLE" in out and "UPDATE" in out and "COUNT(*)" in out


class FakeClient:
    def __init__(self, has_col, gb=1.0, n=7):
        self.has_col, self.gb, self.n, self.ran = has_col, gb, n, []

    def get_table(self, table_id):
        names = ["udi_di"] + (["udi_di_removed_at"] if self.has_col else [])
        return types.SimpleNamespace(schema=[types.SimpleNamespace(name=n) for n in names])

    def query(self, sql, job_config=None):
        if job_config is not None and getattr(job_config, "dry_run", False):
            return types.SimpleNamespace(total_bytes_processed=int(self.gb * 1e9))
        self.ran.append(sql)
        return types.SimpleNamespace(result=lambda: iter([types.SimpleNamespace(n=self.n)]))


def run(monkeypatch, client, *extra):
    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", lambda project: client)
    return B.main(["--project", "p", "--source", "maude", "--from-ingest-date", "2026-11-03", *extra])


def test_without_yes_nothing_is_written(monkeypatch):
    c = FakeClient(has_col=False)
    assert run(monkeypatch, c) == 0
    assert not any(s.startswith(("ALTER", "BEGIN")) for s in c.ran)
    c = FakeClient(has_col=True)
    assert run(monkeypatch, c) == B.EXIT_NOT_CONFIRMED
    assert not any(s.startswith(("ALTER", "BEGIN")) for s in c.ran)   # only the count ran


def test_above_the_cap_nothing_runs(monkeypatch):
    c = FakeClient(has_col=True, gb=99)
    assert run(monkeypatch, c, "--yes") == B.EXIT_OVER_BUDGET
    assert c.ran == []


def test_with_yes_adds_the_column_then_restores(monkeypatch):
    c = FakeClient(has_col=False)
    assert run(monkeypatch, c, "--yes") == 0
    assert c.ran[0].startswith("ALTER TABLE") and c.ran[-1].startswith("BEGIN TRANSACTION")
