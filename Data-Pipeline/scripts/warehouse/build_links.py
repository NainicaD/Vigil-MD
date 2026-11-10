"""Build the exact link tables and the links view: which records in two sources describe the
same thing, by a shared identifier (no ML).

  link_enforcement_recall   enforcement action ↔ recall product, on the FDA recall number
                            (enforcement recall_number = recall product_res_number, the
                            link_key of both tables); event_id ↔ res_event_number is kept as a
                            second check (same_event)
  links (view)              every link table, one row per link, in one shape
                            (schemas.LINK_COLUMNS)

Each link table is written only by its producer and rebuilt in full (CREATE OR REPLACE TABLE
... AS SELECT), so one rebuild never removes another producer's rows. The matching model's
MAUDE ↔ GUDID links are planned as their own table in the same shape; the view then adds it.

Reads vigil_md_prod (enforcement, recall); writes only the link tables and the links view in
the same dataset. --dataset vigil_md_dev builds them on the dev extract.

Checks after the build: no duplicate (left_id, right_id) pair, and at least one link when both
sources have rows. The counts (links, records on either side without a link, links whose
events differ) are logged.

Cost guard: each statement is dry-run first; the build stops above --max-gb, and every job has
maximum_bytes_billed. On PROD (2026-11-10) the join reads about 2 MB (40,449 enforcement and
59,703 recall rows) and writes about 40,000 rows.

--if-changed skips a link table that is newer than both of its sources (table metadata, free).

Exit codes: 0 built or skipped, 2 over budget or bad input, 3 a check failed.

Usage (from Data-Pipeline/):
    python scripts/warehouse/build_links.py --project vigil-md-1792696816 --print-only
    python scripts/warehouse/build_links.py --project vigil-md-1792696816 --estimate-only
    python scripts/warehouse/build_links.py --project vigil-md-1792696816 --if-changed
    python scripts/warehouse/build_links.py --project vigil-md-1792696816 --dataset vigil_md_dev
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import schemas  # noqa: E402

log = get_logger("build_links")

EXIT_OVER_BUDGET = 2
EXIT_CHECK_FAILED = 3
MAX_GB = 1.0
ORDER = list(schemas.LINK_TABLES)


def _check_dataset(dataset: str) -> None:
    """Link tables live next to the tables they link: vigil_md_prod or a vigil_md_dev* extract,
    never RAW, staging or the ops dataset."""
    if dataset != schemas.DATASETS["prod"] and not dataset.startswith("vigil_md_dev"):
        raise ValueError(f"refusing to write link tables to {dataset}: only "
                         f"{schemas.DATASETS['prod']} or vigil_md_dev*")


def _t(project: str, dataset: str, name: str) -> str:
    return f"`{project}.{dataset}.{name}`"


def select_sql(name: str, project: str, dataset: str) -> str:
    """The SELECT that fills one link table, with exactly schemas.LINK_COLUMNS. Pure."""
    if name != "link_enforcement_recall":
        raise ValueError(f"no SQL for link table {name!r}")
    return f"""\
SELECT
  'enforcement_recall' AS link_type,
  'enforcement' AS left_source, e.source_record_id AS left_id,
  'recall' AS right_source, r.source_record_id AS right_id,
  'recall_number' AS link_key,
  e.event_id AS event_id,
  IFNULL(e.event_id = r.res_event_number, FALSE) AS same_event,
  'exact_id' AS method,
  CAST(NULL AS FLOAT64) AS score,
  CAST(NULL AS STRING) AS match_kind,
  CURRENT_TIMESTAMP() AS built_at
FROM {_t(project, dataset, 'enforcement')} AS e
JOIN {_t(project, dataset, 'recall')} AS r ON r.link_key = e.link_key
WHERE e.link_key IS NOT NULL"""


def create_sql(name: str, project: str, dataset: str) -> str:
    """CREATE OR REPLACE TABLE for one link table only. Pure."""
    _check_dataset(dataset)
    target = schemas.link_table(project, name, dataset)
    if name not in schemas.LINK_TABLES:
        raise ValueError(f"refusing to write {name}: not a link table")
    schemas.assert_not_protected(target)
    return (f"CREATE OR REPLACE TABLE `{target}`\nCLUSTER BY link_type, left_id\nAS\n"
            f"{select_sql(name, project, dataset)}")


def view_sql(project: str, dataset: str) -> str:
    """CREATE OR REPLACE VIEW links: every link table, the same columns in the same order. Pure."""
    _check_dataset(dataset)
    target = schemas.link_table(project, schemas.LINKS_VIEW, dataset)
    schemas.assert_not_protected(target)
    cols = ", ".join(c for c, _ in schemas.LINK_COLUMNS)
    union = "\nUNION ALL\n".join(f"SELECT {cols} FROM {_t(project, dataset, n)}" for n in ORDER)
    return f"CREATE OR REPLACE VIEW `{target}` AS\n{union}"


def check_sql(name: str, project: str, dataset: str) -> str:
    """One row of counts after a build: links, duplicates, unlinked records, other events. Pure."""
    left, right = schemas.LINK_TABLES[name]
    return f"""\
SELECT
  (SELECT COUNT(*) FROM {_t(project, dataset, name)}) AS links,
  (SELECT COUNT(*) - COUNT(DISTINCT CONCAT(left_id, '|', right_id))
     FROM {_t(project, dataset, name)}) AS duplicates,
  (SELECT COUNT(*) FROM {_t(project, dataset, left)}) AS left_rows,
  (SELECT COUNT(*) FROM {_t(project, dataset, right)}) AS right_rows,
  (SELECT COUNT(*) FROM {_t(project, dataset, left)} AS x
     WHERE x.link_key IS NULL OR x.link_key NOT IN
       (SELECT left_id FROM {_t(project, dataset, name)})) AS left_unlinked,
  (SELECT COUNT(*) FROM {_t(project, dataset, right)} AS x
     WHERE x.link_key IS NULL OR x.link_key NOT IN
       (SELECT right_id FROM {_t(project, dataset, name)})) AS right_unlinked,
  (SELECT COUNTIF(NOT same_event) FROM {_t(project, dataset, name)}) AS other_event"""


def check_problems(counts: dict) -> list[str]:
    """What is wrong with a built link table, from its check_sql counts. Pure."""
    problems = []
    if counts["duplicates"]:
        problems.append(f"{counts['duplicates']} duplicate (left_id, right_id) pairs")
    if counts["links"] == 0 and counts["left_rows"] and counts["right_rows"]:
        problems.append("no links although both sources have rows")
    return problems


def tables_to_build(source_modified: dict, link_modified: dict) -> list[str]:
    """Link tables that are missing or older than one of their sources. Pure function."""
    out = []
    for name in ORDER:
        mine = link_modified.get(name)
        inputs = [source_modified.get(s) for s in schemas.LINK_TABLES[name]]
        if mine is None or any(t is not None and t > mine for t in inputs):
            out.append(name)
    return out


# ── BigQuery ──────────────────────────────────────────────────────────────────

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
    ap.add_argument("--estimate-only", action="store_true", help="dry runs only (free)")
    ap.add_argument("--if-changed", action="store_true",
                    help="skip link tables newer than both of their sources")
    ap.add_argument("--max-gb", type=float, default=MAX_GB)
    a = ap.parse_args(argv)

    if a.print_only:
        for n in ORDER:
            print(f"-- {n}\n{create_sql(n, a.project, a.dataset)};\n")
        print(f"-- {schemas.LINKS_VIEW}\n{view_sql(a.project, a.dataset)};")
        return 0

    from google.cloud import bigquery
    client = bigquery.Client(project=a.project)
    todo = ORDER
    if a.if_changed and not a.estimate_only:
        sources = sorted({s for pair in schemas.LINK_TABLES.values() for s in pair})
        todo = tables_to_build(_modified(client, a.project, a.dataset, sources),
                               _modified(client, a.project, a.dataset, ORDER))
        if not todo:
            print("=== every link table is newer than its sources: nothing to build ===")
            return 0
        print(f"sources changed: rebuilding {', '.join(todo)}")

    spent = 0.0
    for n in todo:
        gb = client.query(select_sql(n, a.project, a.dataset), job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False)).total_bytes_processed / 1e9
        if a.estimate_only:
            print(f"  {n:24} {gb:7.3f} GB")
            continue
        if spent + gb > a.max_gb:
            log.error("STOP before %s: %.2f GB so far + %.2f GB > --max-gb %.1f",
                      n, spent, gb, a.max_gb)
            return EXIT_OVER_BUDGET
        job = client.query(create_sql(n, a.project, a.dataset), job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=max(10 * 1024 * 1024, int((a.max_gb - spent) * 1e9))))
        job.result()
        spent += (job.total_bytes_billed or 0) / 1e9
        counts = dict(next(iter(client.query(
            check_sql(n, a.project, a.dataset),
            job_config=bigquery.QueryJobConfig(maximum_bytes_billed=200_000_000)).result())))
        log.info("%s: %s", n, counts)
        print(f"  {n:24} {counts['links']:>9,} links; unlinked: {counts['left_unlinked']:,} "
              f"{schemas.LINK_TABLES[n][0]}, {counts['right_unlinked']:,} "
              f"{schemas.LINK_TABLES[n][1]}; other event: {counts['other_event']:,}")
        problems = check_problems(counts)
        if problems:
            log.error("%s failed its checks: %s", n, "; ".join(problems))
            return EXIT_CHECK_FAILED
    if a.estimate_only:
        return 0
    client.query(view_sql(a.project, a.dataset)).result()
    print(f"=== {a.dataset}: {len(todo)} link table(s) built, view {schemas.LINKS_VIEW} "
          f"updated, {spent:.3f} GB billed ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
