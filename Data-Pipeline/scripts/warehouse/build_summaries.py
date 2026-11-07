"""Build the daily summary tables: pre-computed facts per device, product code and manufacturer.

The chatbot and the website read a few rows of these instead of scanning maude for every
question (3.5-4.7 GB per question without them). Each table is rebuilt in full with one
CREATE OR REPLACE TABLE ... AS SELECT, in this order:

  device_catalog   GUDID records grouped by company and brand name (one "device"), with its
                   UDIs, model numbers and product codes; device_id = 'dev:' + 12 hex of
                   SHA-1(company_norm|brand_key), the same id as the chatbot's search index
  entity_reports   entity × report: which reports belong to each device (exact UDI links),
                   product code and manufacturer; event type and date per report
  entity_counts    entity × year × event type → reports
  entity_problems  entity × top problem terms (no-information terms left out), plus one
                   'no_harm' row with the reports that say no patient harm
  entity_recalls   entity × recall with class, reason, root cause and action: by product code,
                   by manufacturer name, and for a device the recalls of its product codes
                   whose product text contains the device's name
  entity_catalog   entity: title, company, product codes, top brand names, totals (what the
                   search index needs)

Reads vigil_md_prod (gudid, maude, maude_problems, recall, enforcement), writes only the
summary tables in the same dataset. --dataset vigil_md_dev builds them on the dev extract.

Cost guard: each statement is dry-run just before it runs (later tables read earlier ones,
so they can only be priced once those exist); the build stops if the running total would
pass --max-gb, and every job has maximum_bytes_billed.

--if-changed rebuilds only the tables whose inputs changed since they were built (table
metadata, free). Recalls change almost daily, MAUDE about weekly: a recall-only day
rebuilds entity_recalls (about 0.6 GB on PROD), a MAUDE day about 11 GB, a new GUDID
release everything (about 12 GB).

Exit codes: 0 built or skipped, 2 over budget or bad input, 3 a check failed (duplicate ids).

Usage (from Data-Pipeline/):
    python scripts/warehouse/build_summaries.py --project vigil-md-1792696816 --print-only
    python scripts/warehouse/build_summaries.py --project vigil-md-1792696816 --estimate-only
    python scripts/warehouse/build_summaries.py --project vigil-md-1792696816 --if-changed
    python scripts/warehouse/build_summaries.py --project vigil-md-1792696816 --dataset vigil_md_dev
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import schemas  # noqa: E402
from scripts.warehouse import summary_config as cfg  # noqa: E402

log = get_logger("build_summaries")

EXIT_OVER_BUDGET = 2
EXIT_CHECK_FAILED = 3
ORDER = list(schemas.SUMMARY_TABLES)          # build order
# What each summary table reads: earlier summary tables (NEEDS: also when it can be
# dry-run) and prod source tables (SOURCES). A table is rebuilt when any input is newer.
NEEDS = {
    "device_catalog": [],
    "entity_reports": ["device_catalog"],
    "entity_counts": ["entity_reports"],
    "entity_problems": ["entity_reports"],
    "entity_recalls": ["device_catalog"],
    "entity_catalog": ["device_catalog", "entity_counts"],
}
SOURCES = {
    "device_catalog": ["gudid"],
    "entity_reports": ["maude"],
    "entity_counts": [],
    "entity_problems": ["maude_problems"],
    "entity_recalls": ["recall", "enforcement"],
    "entity_catalog": ["maude"],
}
_NORM = r"r'[^a-z0-9]+'"     # the same normalization as the chatbot's device search


def _t(project: str, dataset: str, name: str) -> str:
    return f"`{project}.{dataset}.{name}`"


# ── SQL (pure) ────────────────────────────────────────────────────────────────

def select_sql(name: str, project: str, dataset: str) -> tuple[str, dict]:
    """The SELECT for one summary table and its parameters {name: (type, value)}."""
    t = lambda n: _t(project, dataset, n)          # noqa: E731
    if name == "device_catalog":
        return f"""\
WITH g AS (
  SELECT IFNULL(manufacturer_name_norm, '') AS company_norm, manufacturer_name_raw AS company,
         TRIM(device_name_raw) AS name,
         TRIM(REGEXP_REPLACE(LOWER(device_name_raw), {_NORM}, ' ')) AS brand_key,
         NULLIF(TRIM(model_number_raw), '') AS model, udi_di, product_codes
  FROM {t('gudid')}
  WHERE TRIM(IFNULL(device_name_raw, '')) != ''
),
named AS (
  SELECT company_norm, brand_key, ARRAY_AGG(name ORDER BY n DESC, name LIMIT 1)[OFFSET(0)] AS name
  FROM (SELECT company_norm, brand_key, name, COUNT(*) AS n FROM g GROUP BY 1, 2, 3)
  GROUP BY 1, 2
),
firms AS (
  SELECT company_norm, brand_key,
         ARRAY_AGG(company IGNORE NULLS ORDER BY n DESC, company LIMIT 1)[SAFE_OFFSET(0)] AS company
  FROM (SELECT company_norm, brand_key, company, COUNT(*) AS n FROM g GROUP BY 1, 2, 3)
  GROUP BY 1, 2
),
models AS (
  SELECT company_norm, brand_key, ARRAY_AGG(model ORDER BY n DESC, model LIMIT @top_models) AS models,
         COUNT(*) AS n_models
  FROM (SELECT company_norm, brand_key, model, COUNT(*) AS n FROM g WHERE model IS NOT NULL GROUP BY 1, 2, 3)
  GROUP BY 1, 2
),
codes AS (
  SELECT company_norm, brand_key, ARRAY_AGG(DISTINCT pc ORDER BY pc) AS product_codes
  FROM g, UNNEST(product_codes) AS pc
  GROUP BY 1, 2
),
udis AS (
  SELECT company_norm, brand_key, ARRAY_AGG(DISTINCT udi_di ORDER BY udi_di) AS udi_dis
  FROM g WHERE udi_di IS NOT NULL
  GROUP BY 1, 2
)
SELECT CONCAT('dev:', SUBSTR(TO_HEX(SHA1(CONCAT(n.company_norm, '|', n.brand_key))), 1, 12)) AS device_id,
       n.company_norm, n.brand_key, n.name, f.company,
       IFNULL(c.product_codes, []) AS product_codes, IFNULL(u.udi_dis, []) AS udi_dis,
       IFNULL(m.models, []) AS models, IFNULL(m.n_models, 0) AS n_models,
       ARRAY_LENGTH(IFNULL(u.udi_dis, [])) AS n_udis
FROM named AS n
JOIN firms AS f USING (company_norm, brand_key)
LEFT JOIN models AS m USING (company_norm, brand_key)
LEFT JOIN codes AS c USING (company_norm, brand_key)
LEFT JOIN udis AS u USING (company_norm, brand_key)""", {"top_models": ("INT64", cfg.TOP_MODELS)}

    if name == "entity_reports":
        return f"""\
WITH maude_rows AS (
  SELECT source_record_id, event_type, record_date, product_code, manufacturer_name_norm, udi_di
  FROM {t('maude')}
),
reports AS (
  SELECT source_record_id, ANY_VALUE(event_type) AS event_type, MIN(record_date) AS record_date
  FROM maude_rows GROUP BY source_record_id
),
udi_device AS (
  SELECT device_id, udi FROM {t('device_catalog')}, UNNEST(udi_dis) AS udi
),
links AS (
  SELECT 'product_code' AS entity_type, product_code AS entity_id, source_record_id
  FROM maude_rows WHERE product_code IS NOT NULL
  UNION DISTINCT
  SELECT 'manufacturer', manufacturer_name_norm, source_record_id
  FROM maude_rows WHERE manufacturer_name_norm IS NOT NULL
  UNION DISTINCT
  SELECT 'device', d.device_id, r.source_record_id
  FROM maude_rows AS r JOIN udi_device AS d ON d.udi = r.udi_di
)
SELECT l.entity_type, l.entity_id, l.source_record_id, r.event_type, r.record_date
FROM links AS l JOIN reports AS r USING (source_record_id)""", {}

    if name == "entity_counts":
        return f"""\
SELECT entity_type, entity_id, EXTRACT(YEAR FROM record_date) AS year,
       IFNULL(event_type, '(none)') AS event_type, COUNT(*) AS reports
FROM {t('entity_reports')}
GROUP BY entity_type, entity_id, year, event_type""", {}

    if name == "entity_problems":
        return f"""\
WITH ep AS (
  SELECT er.entity_type, er.entity_id, er.source_record_id, p.problem_type, p.problem_term
  FROM {t('entity_reports')} AS er
  JOIN {t('maude_problems')} AS p ON p.mdr_report_key = er.source_record_id
),
ranked AS (
  SELECT entity_type, entity_id, problem_type, problem_term,
         COUNT(DISTINCT source_record_id) AS reports
  FROM ep
  WHERE problem_term NOT IN UNNEST(@no_information) AND problem_term NOT IN UNNEST(@no_harm)
  GROUP BY entity_type, entity_id, problem_type, problem_term
)
(SELECT entity_type, entity_id, problem_type, problem_term, reports,
        ROW_NUMBER() OVER (PARTITION BY entity_type, entity_id, problem_type
                           ORDER BY reports DESC, problem_term) AS rank
 FROM ranked
 WHERE TRUE
 QUALIFY rank <= @top_terms)
UNION ALL
(SELECT entity_type, entity_id, 'no_harm' AS problem_type, CAST(NULL AS STRING) AS problem_term,
        COUNT(DISTINCT source_record_id) AS reports, 0 AS rank
 FROM ep
 WHERE problem_type = 'patient' AND problem_term IN UNNEST(@no_harm)
 GROUP BY entity_type, entity_id)""", {
            "no_information": ("ARRAY<STRING>", list(cfg.NO_INFORMATION)),
            "no_harm": ("ARRAY<STRING>", list(cfg.NO_HARM)),
            "top_terms": ("INT64", cfg.TOP_TERMS)}

    if name == "entity_recalls":
        return f"""\
WITH r AS (
  SELECT r.source_record_id AS recall_number, e.recall_class, r.record_date, r.recall_status,
         r.device_name_raw AS product, r.manufacturer_name_raw AS firm, r.reason_text,
         r.root_cause, r.action, r.product_code, r.manufacturer_name_norm,
         CONCAT(' ', TRIM(REGEXP_REPLACE(LOWER(IFNULL(r.device_name_raw, '')), {_NORM}, ' ')), ' ') AS words
  FROM {t('recall')} AS r
  LEFT JOIN {t('enforcement')} AS e ON e.source_record_id = r.source_record_id
),
links AS (
  SELECT 'product_code' AS entity_type, product_code AS entity_id, recall_number,
         'product_code' AS matched_by
  FROM r WHERE product_code IS NOT NULL
  UNION ALL
  SELECT 'manufacturer', manufacturer_name_norm, recall_number, 'manufacturer'
  FROM r WHERE manufacturer_name_norm IS NOT NULL
  UNION ALL
  SELECT DISTINCT 'device', d.device_id, r.recall_number, 'device_name'
  FROM {t('device_catalog')} AS d, UNNEST(d.product_codes) AS pc
  JOIN r ON r.product_code = pc
  WHERE LENGTH(d.brand_key) >= @min_brand_chars
    AND STRPOS(r.words, CONCAT(' ', d.brand_key, ' ')) > 0
)
SELECT l.entity_type, l.entity_id, l.matched_by, r.recall_number, r.recall_class, r.record_date,
       r.recall_status, r.product, r.firm, r.reason_text, r.root_cause, r.action
FROM links AS l JOIN r USING (recall_number)""", {"min_brand_chars": ("INT64", cfg.MIN_BRAND_CHARS)}

    if name == "entity_catalog":
        return f"""\
WITH totals AS (
  SELECT entity_type, entity_id, SUM(reports) AS reports,
         SUM(IF(event_type = 'Death', reports, 0)) AS deaths,
         SUM(IF(event_type = 'Injury', reports, 0)) AS injuries,
         SUM(IF(event_type = 'Malfunction', reports, 0)) AS malfunctions
  FROM {t('entity_counts')}
  GROUP BY entity_type, entity_id
),
names AS (
  SELECT product_code, manufacturer_name_norm, TRIM(device_name_raw) AS name,
         manufacturer_name_raw AS firm, source_record_id
  FROM {t('maude')}
),
code_names AS (
  SELECT product_code, ARRAY_AGG(name ORDER BY n DESC, name LIMIT @top_names) AS top_names
  FROM (SELECT product_code, name, COUNT(DISTINCT source_record_id) AS n FROM names
        WHERE product_code IS NOT NULL AND name IS NOT NULL AND name != '' GROUP BY 1, 2)
  GROUP BY product_code
),
firms AS (
  SELECT manufacturer_name_norm,
         ARRAY_AGG(firm IGNORE NULLS ORDER BY n DESC, firm LIMIT 1)[SAFE_OFFSET(0)] AS firm,
         ARRAY_CONCAT_AGG(codes) AS codes
  FROM (SELECT manufacturer_name_norm, firm, COUNT(DISTINCT source_record_id) AS n,
               ARRAY_AGG(DISTINCT product_code IGNORE NULLS) AS codes
        FROM names WHERE manufacturer_name_norm IS NOT NULL GROUP BY 1, 2)
  GROUP BY manufacturer_name_norm
)
SELECT 'device' AS entity_type, d.device_id AS entity_id, d.name AS title, d.company AS subtitle,
       d.company_norm, d.product_codes, CAST([] AS ARRAY<STRING>) AS top_names,
       IFNULL(t.reports, 0) AS reports, IFNULL(t.deaths, 0) AS deaths,
       IFNULL(t.injuries, 0) AS injuries, IFNULL(t.malfunctions, 0) AS malfunctions
FROM {t('device_catalog')} AS d
LEFT JOIN totals AS t ON t.entity_type = 'device' AND t.entity_id = d.device_id
UNION ALL
SELECT 'manufacturer', f.manufacturer_name_norm, IFNULL(f.firm, f.manufacturer_name_norm),
       CAST(NULL AS STRING), f.manufacturer_name_norm,
       ARRAY(SELECT DISTINCT c FROM UNNEST(f.codes) AS c ORDER BY c), CAST([] AS ARRAY<STRING>),
       IFNULL(t.reports, 0), IFNULL(t.deaths, 0), IFNULL(t.injuries, 0), IFNULL(t.malfunctions, 0)
FROM firms AS f
LEFT JOIN totals AS t ON t.entity_type = 'manufacturer' AND t.entity_id = f.manufacturer_name_norm
UNION ALL
SELECT 'product_code', c.product_code, CONCAT('Product code ', c.product_code),
       ARRAY_TO_STRING(c.top_names, '; '), CAST(NULL AS STRING), [c.product_code], c.top_names,
       IFNULL(t.reports, 0), IFNULL(t.deaths, 0), IFNULL(t.injuries, 0), IFNULL(t.malfunctions, 0)
FROM code_names AS c
LEFT JOIN totals AS t ON t.entity_type = 'product_code' AND t.entity_id = c.product_code""", {
            "top_names": ("INT64", cfg.TOP_NAMES)}
    raise ValueError(f"unknown summary table {name!r}")


CLUSTER = {
    "device_catalog": "device_id",
    "entity_reports": "entity_type, entity_id",
    "entity_counts": "entity_type, entity_id",
    "entity_problems": "entity_type, entity_id",
    "entity_recalls": "entity_type, entity_id",
    "entity_catalog": "entity_type, entity_id",
}


def create_sql(name: str, project: str, dataset: str) -> tuple[str, dict]:
    """CREATE OR REPLACE TABLE <dataset>.<name> AS <select>. Only summary tables."""
    if name not in schemas.SUMMARY_TABLES:
        raise ValueError(f"not a summary table: {name}")
    target = schemas.summary_table(project, name, dataset)
    schemas.assert_not_protected(target)
    select, params = select_sql(name, project, dataset)
    return (f"CREATE OR REPLACE TABLE `{target}`\nCLUSTER BY {CLUSTER[name]}\n"
            f"OPTIONS (description = 'Daily summary table, built by "
            f"scripts/warehouse/build_summaries.py')\nAS\n{select}"), params


def duplicate_id_sql(project: str, dataset: str) -> str:
    return (f"SELECT COUNT(*) - COUNT(DISTINCT device_id) AS duplicates "
            f"FROM {_t(project, dataset, 'device_catalog')}")


def tables_to_build(source_modified: dict, summary_modified: dict) -> list[str]:
    """The summary tables to rebuild, in build order: missing ones, ones older than one of
    their inputs, and ones whose input summary table is rebuilt in this run. Pure function."""
    out = []
    for n in ORDER:
        mine = summary_modified.get(n)
        inputs = [source_modified.get(s) for s in SOURCES[n]] + \
                 [summary_modified.get(d) for d in NEEDS[n]]
        if (mine is None or any(d in out for d in NEEDS[n])
                or any(t is not None and t > mine for t in inputs)):
            out.append(n)
    return out


# ── BigQuery ──────────────────────────────────────────────────────────────────

def _params(params: dict) -> list:
    from google.cloud import bigquery
    return [bigquery.ArrayQueryParameter(k, t[6:-1], v) if t.startswith("ARRAY<")
            else bigquery.ScalarQueryParameter(k, t, v) for k, (t, v) in params.items()]


def _modified(client, project: str, dataset: str, names) -> dict:
    from google.api_core.exceptions import NotFound
    out = {}
    for n in names:
        try:
            out[n] = client.get_table(f"{project}.{dataset}.{n}").modified
        except NotFound:
            out[n] = None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default=schemas.DATASETS["prod"])
    ap.add_argument("--print-only", action="store_true", help="print the SQL; no GCP call")
    ap.add_argument("--estimate-only", action="store_true",
                    help="dry runs only (free); tables whose inputs don't exist yet are skipped")
    ap.add_argument("--if-changed", action="store_true",
                    help="skip when no source table changed since the last build")
    ap.add_argument("--max-gb", type=float, default=cfg.MAX_GB)
    a = ap.parse_args(argv)

    if a.print_only:
        for n in ORDER:
            sql, params = create_sql(n, a.project, a.dataset)
            print(f"-- {n}  params: { {k: v for k, (_, v) in params.items()} }\n{sql};\n")
        return 0

    from google.cloud import bigquery
    client = bigquery.Client(project=a.project)
    existing = _modified(client, a.project, a.dataset, ORDER)

    todo = ORDER
    if a.if_changed and not a.estimate_only:
        sources = _modified(client, a.project, a.dataset, schemas.SUMMARY_SOURCES)
        todo = tables_to_build(sources, existing)
        if not todo:
            print("=== every summary table is newer than its inputs: nothing to build ===")
            return 0
        print(f"inputs changed: rebuilding {', '.join(todo)}")

    spent, built = 0.0, set()
    for n in todo:
        sql, params = create_sql(n, a.project, a.dataset)
        ready = all(existing.get(d) is not None or d in built for d in NEEDS[n])
        if not ready:
            print(f"  {n:16}   n/a      (priced once {', '.join(NEEDS[n])} exist)")
            continue
        gb = client.query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, query_parameters=_params(params))).total_bytes_processed / 1e9
        if a.estimate_only:
            print(f"  {n:16} {gb:7.3f} GB")
            continue
        if spent + gb > a.max_gb:
            log.error("STOP before %s: %.2f GB so far + %.2f GB > --max-gb %.1f. "
                      "Tables built so far are kept.", n, spent, gb, a.max_gb)
            return EXIT_OVER_BUDGET
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            query_parameters=_params(params),
            maximum_bytes_billed=max(10 * 1024 * 1024, int((a.max_gb - spent) * 1e9))))
        job.result()
        spent += (job.total_bytes_billed or 0) / 1e9
        built.add(n)
        tbl = client.get_table(schemas.summary_table(a.project, n, a.dataset))
        print(f"  {n:16} {tbl.num_rows:>12,} rows {tbl.num_bytes / 1e6:9.1f} MB  "
              f"billed {(job.total_bytes_billed or 0) / 1e9:.3f} GB")
        if n == "device_catalog":
            dup = next(iter(client.query(duplicate_id_sql(a.project, a.dataset)).result())).duplicates
            if dup:
                log.error("device_catalog has %d duplicate device_id values: stopping", dup)
                return EXIT_CHECK_FAILED
    if not a.estimate_only:
        print(f"=== {a.dataset}: {len(built)} summary tables built, {spent:.2f} GB billed ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
