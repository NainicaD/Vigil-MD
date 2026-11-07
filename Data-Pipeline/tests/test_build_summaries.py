"""Tests for scripts/warehouse/build_summaries.py (the daily summary tables).

SQL is checked as text; BigQuery is a fake client (no GCP).
"""
import datetime as dt
import hashlib
import re
import sys
import types

import pytest

from scripts.warehouse import build_summaries as S
from scripts.warehouse import schemas
from scripts.warehouse import summary_config as cfg

P, DS = "p", "vigil_md_prod"


# ── SQL ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", list(schemas.SUMMARY_TABLES))
def test_every_table_selects_its_schema_columns(name):
    sql, _ = S.create_sql(name, P, DS)
    for col, _ in schemas.SUMMARY_TABLES[name]:
        assert re.search(rf"\b{col}\b", sql), (name, col)


@pytest.mark.parametrize("name", list(schemas.SUMMARY_TABLES))
def test_tables_are_partitioned_by_entity_bucket(name):
    sql, _ = S.create_sql(name, P, DS)
    assert f"PARTITION BY RANGE_BUCKET(bucket, GENERATE_ARRAY(0, {cfg.BUCKETS}, 1))" in sql
    expected = (cfg.bucket_sql("'device'", "device_id") if name == "device_catalog"
                else cfg.bucket_sql("entity_type", "entity_id"))
    assert f"SELECT *, {expected} AS bucket" in sql


def test_bucket_matches_bigquery():
    # values computed by BigQuery with bucket_sql() on 2026-11-07
    keys = [("device", "dev:f0cdc15b4d63"), ("product_code", "LZG"), ("manufacturer", "respironics"),
            ("manufacturer", "medtronic minimed"), ("product_code", "ZZZ")]
    assert [cfg.entity_bucket(t, i) for t, i in keys] == [428, 627, 574, 407, 712]
    assert all(0 <= cfg.entity_bucket("device", f"dev:{n}") < cfg.BUCKETS for n in range(200))


@pytest.mark.parametrize("name", list(schemas.SUMMARY_TABLES))
def test_create_writes_only_its_summary_table_in_the_dataset(name):
    sql, _ = S.create_sql(name, P, DS)
    assert sql.startswith(f"CREATE OR REPLACE TABLE `{P}.{DS}.{name}`")
    assert sql.count("CREATE") == 1 and "INSERT" not in sql and "DELETE" not in sql
    for src in schemas.SUMMARY_SOURCES:                 # sources are only read
        assert f"CREATE OR REPLACE TABLE `{P}.{DS}.{src}`" not in sql


# Reserved words BigQuery refuses as names (a CTE "rows" broke the first dev build)
RESERVED = {"rows", "range", "groups", "rollup", "window", "lookup", "hash", "within",
            "contains", "partition", "proto", "struct", "array", "default"}


@pytest.mark.parametrize("name", list(schemas.SUMMARY_TABLES))
def test_no_reserved_word_is_used_as_a_name(name):
    sql, _ = S.select_sql(name, P, DS)
    names = re.findall(r"(?:WITH|,)\s*(\w+)\s+AS\s*\(", sql) + re.findall(r"\bAS\s+(\w+)\b(?!\s*<)", sql)   # not CAST(... AS ARRAY<...>)
    assert not {n.lower() for n in names} & RESERVED, (name, names)


def test_unknown_or_protected_targets_are_refused():
    with pytest.raises(ValueError):
        S.create_sql("maude", P, DS)
    with pytest.raises(ValueError):
        S.create_sql("device_catalog", P, "vigil_md_processed")


def test_build_order_respects_what_each_table_reads():
    for name, needs in S.NEEDS.items():
        assert all(S.ORDER.index(d) < S.ORDER.index(name) for d in needs), name
        sql, _ = S.select_sql(name, P, DS)
        for other in schemas.SUMMARY_TABLES:
            if f"`{P}.{DS}.{other}`" in sql:
                assert other in needs, (name, other)


def test_device_id_matches_the_chatbot_search_index():
    # chatbot device_search: "dev:" + sha1(f"{company_norm}|{brand_key}").hexdigest()[:12]
    sql, _ = S.select_sql("device_catalog", P, DS)
    assert "CONCAT('dev:', SUBSTR(TO_HEX(SHA1(CONCAT(n.company_norm, '|', n.brand_key))), 1, 12))" in sql
    assert "dev:" + hashlib.sha1(b"respironics|dreamstation").hexdigest()[:12] == "dev:f0cdc15b4d63"


def test_brand_key_uses_the_search_index_normalization():
    sql, _ = S.select_sql("device_catalog", P, DS)
    assert "TRIM(REGEXP_REPLACE(LOWER(device_name_raw), r'[^a-z0-9]+', ' ')) AS brand_key" in sql


def test_reports_count_once_per_entity():
    sql, _ = S.select_sql("entity_reports", P, DS)
    assert sql.count("UNION DISTINCT") == 2 and "GROUP BY source_record_id" in sql
    counts, _ = S.select_sql("entity_counts", P, DS)
    assert "COUNT(*) AS reports" in counts and "IFNULL(event_type, '(none)')" in counts


def test_device_reports_come_from_exact_udi_links():
    sql, _ = S.select_sql("entity_reports", P, DS)
    assert "UNNEST(udi_dis) AS udi" in sql and "ON d.udi = r.udi_di" in sql


def test_problem_terms_are_parameters_and_ranked():
    sql, params = S.select_sql("entity_problems", P, DS)
    assert params["no_information"] == ("ARRAY<STRING>", list(cfg.NO_INFORMATION))
    assert params["no_harm"] == ("ARRAY<STRING>", list(cfg.NO_HARM))
    assert params["top_terms"] == ("INT64", cfg.TOP_TERMS)
    assert "QUALIFY rank <= @top_terms" in sql and "'no_harm' AS problem_type" in sql
    for term in cfg.NO_INFORMATION + cfg.NO_HARM:      # lists are bound, not pasted
        assert term not in sql


def test_device_recalls_need_a_shared_product_code_and_the_name_in_the_text():
    sql, params = S.select_sql("entity_recalls", P, DS)
    assert "JOIN r ON r.product_code = pc" in sql
    assert "STRPOS(r.words, CONCAT(' ', d.brand_key, ' ')) > 0" in sql
    assert params["min_brand_chars"] == ("INT64", cfg.MIN_BRAND_CHARS)
    assert {"'product_code'", "'manufacturer'", "'device_name'"} <= set(re.findall(r"'[a-z_]+'", sql))


def test_catalog_totals_come_from_entity_counts():
    sql, _ = S.select_sql("entity_catalog", P, DS)
    assert f"FROM `{P}.{DS}.entity_counts`" in sql
    for et in schemas.ENTITY_TYPES:
        assert f"'{et}'" in sql


# ── staleness ─────────────────────────────────────────────────────────────────

T0 = dt.datetime(2026, 11, 7, 6, 0)


FRESH = {n: T0 + dt.timedelta(hours=1) for n in S.ORDER}
SOURCES0 = {n: T0 for n in schemas.SUMMARY_SOURCES}
LATER = T0 + dt.timedelta(hours=2)


def test_every_source_feeds_some_summary_table():
    used = {s for srcs in S.SOURCES.values() for s in srcs}
    assert used == set(schemas.SUMMARY_SOURCES) and set(S.SOURCES) == set(S.ORDER)


@pytest.mark.parametrize("changed, expected", [
    ({}, []),
    ({"recall": LATER}, ["entity_recalls"]),                         # the usual day
    ({"enforcement": LATER}, ["entity_recalls"]),
    ({"maude_problems": LATER}, ["entity_problems"]),
    ({"maude": LATER}, ["entity_reports", "entity_counts", "entity_problems", "entity_catalog"]),
    ({"gudid": LATER}, S.ORDER),                                     # new GUDID: everything
])
def test_only_tables_whose_inputs_changed_are_rebuilt(changed, expected):
    assert S.tables_to_build({**SOURCES0, **changed}, FRESH) == expected


def test_missing_summary_tables_and_their_dependents_are_built():
    assert S.tables_to_build(SOURCES0, {**FRESH, "entity_reports": None}) == \
        ["entity_reports", "entity_counts", "entity_problems", "entity_catalog"]
    assert S.tables_to_build(SOURCES0, {}) == S.ORDER


def test_sources_listed_match_the_sql():
    for name in S.ORDER:
        sql, _ = S.select_sql(name, P, DS)
        read = {src for src in schemas.SUMMARY_SOURCES if f"`{P}.{DS}.{src}`" in sql}
        assert read == set(S.SOURCES[name]), (name, read)


# ── main (fake BigQuery) ──────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, modified=None, dry_gb=0.1, duplicates=0):
        self.modified = modified or {}
        self.dry_gb, self.duplicates = dry_gb, duplicates
        self.created, self.dropped = [], []

    def get_table(self, table_id):
        from google.api_core.exceptions import NotFound
        name = table_id.rsplit(".", 1)[-1]
        if name in S.ORDER and name in self.created:
            return types.SimpleNamespace(modified=T0, num_rows=10, num_bytes=1000)
        if self.modified.get(name) is None:
            raise NotFound(name)
        return types.SimpleNamespace(modified=self.modified[name], num_rows=10, num_bytes=1000)

    def query(self, sql, job_config=None):
        if job_config is not None and job_config.dry_run:
            return types.SimpleNamespace(total_bytes_processed=int(self.dry_gb * 1e9))
        if sql.startswith("DROP TABLE IF EXISTS"):
            self.dropped.append(sql.rsplit(".", 1)[-1].rstrip("`"))
            return types.SimpleNamespace(result=lambda: None)
        if sql.startswith("SELECT COUNT(*) - COUNT(DISTINCT device_id)"):
            return types.SimpleNamespace(result=lambda: iter([types.SimpleNamespace(duplicates=self.duplicates)]))
        name = re.search(r"TABLE `[^`]*\.(\w+)`", sql).group(1)
        self.created.append(name)
        return types.SimpleNamespace(result=lambda: None, total_bytes_billed=int(self.dry_gb * 1e9),
                                     cap=job_config.maximum_bytes_billed)


@pytest.fixture
def client(monkeypatch):
    holder = {}

    def make(**kw):
        c = FakeClient(**kw)
        holder["c"] = c
        return c
    from google.cloud import bigquery
    monkeypatch.setattr(bigquery, "Client", lambda project: holder["c"])
    return make


def run(*extra):
    return S.main(["--project", P, *extra])


def test_build_creates_every_table_in_order(client, capsys):
    c = client(modified={s: T0 for s in schemas.SUMMARY_SOURCES})
    assert run() == 0
    assert c.created == S.ORDER
    assert "6 summary tables built" in capsys.readouterr().out


def test_if_changed_skips_when_summaries_are_newer(client, capsys):
    newer = T0 + dt.timedelta(hours=1)
    c = client(modified={**{s: T0 for s in schemas.SUMMARY_SOURCES}, **{n: newer for n in S.ORDER}})
    assert run("--if-changed") == 0 and c.created == []
    assert "nothing to build" in capsys.readouterr().out


def test_if_changed_builds_when_a_source_is_newer(client):
    old = T0 - dt.timedelta(days=1)
    c = client(modified={**{s: T0 for s in schemas.SUMMARY_SOURCES}, **{n: old for n in S.ORDER}})
    assert run("--if-changed") == 0 and c.created == S.ORDER


def test_if_changed_on_a_recall_only_day_rebuilds_entity_recalls_only(client, capsys):
    newer = T0 + dt.timedelta(hours=1)
    c = client(modified={**{s: T0 for s in schemas.SUMMARY_SOURCES}, **{n: newer for n in S.ORDER},
                         "recall": newer + dt.timedelta(hours=1)})
    assert run("--if-changed") == 0 and c.created == ["entity_recalls"]
    assert "rebuilding entity_recalls" in capsys.readouterr().out


def test_stops_before_passing_the_budget(client):
    c = client(modified={s: T0 for s in schemas.SUMMARY_SOURCES}, dry_gb=4.0)
    assert run("--max-gb", "10") == S.EXIT_OVER_BUDGET
    assert c.created == ["device_catalog", "entity_reports"]       # 8 GB built, 12 would pass 10


def test_duplicate_device_ids_stop_the_build(client):
    c = client(modified={s: T0 for s in schemas.SUMMARY_SOURCES}, duplicates=2)
    assert run() == S.EXIT_CHECK_FAILED and c.created == ["device_catalog"]


def test_pricing_reads_only_the_select():
    sql, _ = S.create_sql("entity_counts", P, DS)
    priced = S.priced_sql(sql)
    assert priced.startswith("SELECT *, MOD(") and "CREATE" not in priced and "PARTITION BY" not in priced.split("FROM (", 1)[0]


def test_drop_only_summary_tables():
    assert S.drop_sql("entity_reports", P, DS) == f"DROP TABLE IF EXISTS `{P}.{DS}.entity_reports`"
    for name in ("maude", "recall", "gudid", "load_log"):
        with pytest.raises(ValueError):
            S.drop_sql(name, P, DS)


def test_recreate_needs_confirmation(client, capsys):
    c = client(modified={s: T0 for s in schemas.SUMMARY_SOURCES})
    assert run("--recreate") == S.EXIT_OVER_BUDGET and c.created == [] and c.dropped == []
    assert "--yes-recreate" in capsys.readouterr().out


def test_recreate_drops_each_summary_table_before_building_it(client):
    c = client(modified={s: T0 for s in schemas.SUMMARY_SOURCES})
    assert run("--recreate", "--yes-recreate") == 0
    assert c.dropped == S.ORDER and c.created == S.ORDER


def test_estimate_only_creates_nothing(client, capsys):
    c = client(modified={s: T0 for s in schemas.SUMMARY_SOURCES})
    assert run("--estimate-only") == 0 and c.created == []
    assert "priced once device_catalog exist" in capsys.readouterr().out


def test_print_only_makes_no_gcp_call(monkeypatch, capsys):
    for mod in [m for m in sys.modules if m.startswith("google.cloud")]:
        monkeypatch.delitem(sys.modules, mod)
    assert run("--print-only") == 0
    assert capsys.readouterr().out.count("CREATE OR REPLACE TABLE") == 6
    assert not any(m.startswith("google.cloud") for m in sys.modules)
