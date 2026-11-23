"""Load the matching model's MAUDE ↔ GUDID links (s12_score_pairs.py output) into a link table.

  link_maude_gudid_model   one row per MATCH, columns schemas.LINK_COLUMNS, method 'model'
  links (view)             rebuilt with the exact link tables plus this one

Default --dataset is vigil_md_dev (the test extract). vigil_md_prod is written only when it is
named explicitly. Only vigil_md_prod and vigil_md_dev* are allowed (build_links._check_dataset).

Steps:
  1. create the link table if it does not exist (CLUSTER BY link_type, left_id)
  2. load the parquet into vigil_md_staging.link_maude_gudid_model_load_<dataset> (load job, free)
  3. one transaction: delete the links of every MAUDE row that s13 selected again
     (vigil_md_model.match_candidates_<dataset>; --full: every link), insert the new links,
     ROLLBACK + RAISE if a (left_id, right_id) pair occurs twice
  4. checks: no duplicate pair, method 'model' on every row, every score in [0, 1]
  5. rebuild the links view
  6. ELK records: one "matching_chunk_scored" per line of s12's chunks.jsonl (--chunks), and one
     "matching_run_metrics": the s12 report (--report), link counts, run_id, and storage sizes
     (link and candidate table bytes from table metadata, no query cost; run folder bytes)

shortcut: the incremental delete only reaches MAUDE rows that s13 selected again, so a link whose
MAUDE row later lost every candidate (or got an exact UDI link) stays until the next --full run.
Run --full after each new model version.

Cost guard: every job has maximum_bytes_billed (--max-gb). A transaction script cannot be
dry-run, so the cap is the guard; it reads the link table, the candidates and the load table.

Exit codes: 0 loaded, 2 bad input, 3 a check failed (no retry in Airflow).

Usage (from Data-Pipeline/):
    python scripts/warehouse/load_model_links.py --project vigil-md-1792696816 --print-only
    python scripts/warehouse/load_model_links.py --project vigil-md-1792696816 \
        --links /tmp/links.parquet --report /tmp/report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import schemas  # noqa: E402
from scripts.warehouse.build_links import ORDER, _check_dataset, view_sql  # noqa: E402

log = get_logger("load_model_links")

EXIT_BAD_INPUT = 2
EXIT_CHECK_FAILED = 3
MAX_GB = 5.0
TABLE = schemas.MODEL_LINK_TABLE
CHECK_MAX_BYTES = 200_000_000


# ── Table names and SQL (pure) ────────────────────────────────────────────────

def target(project: str, dataset: str) -> str:
    _check_dataset(dataset)
    t = schemas.link_table(project, TABLE, dataset)
    schemas.assert_not_protected(t)
    return t


def load_table(project: str, dataset: str) -> str:
    return f"{project}.{schemas.DATASETS['staging']}.{TABLE}_load_{dataset}"


def candidates_table(project: str, dataset: str) -> str:
    """Written by Model-Development/matching/scripts/s13_build_candidates.py."""
    return f"{project}.vigil_md_model.match_candidates_{dataset}"


def create_sql(project: str, dataset: str) -> str:
    cols = ",\n  ".join(f"{c} {t}" for c, t in schemas.LINK_COLUMNS)
    return (f"CREATE TABLE IF NOT EXISTS `{target(project, dataset)}` (\n  {cols}\n)\n"
            f"CLUSTER BY link_type, left_id")


def merge_sql(project: str, dataset: str, full: bool) -> str:
    """One transaction: delete the re-scored rows' links, insert the new ones, dup check."""
    t = target(project, dataset)
    cols = ", ".join(c for c, _ in schemas.LINK_COLUMNS)
    scope = "TRUE" if full else f"left_id IN (SELECT maude_id FROM `{candidates_table(project, dataset)}`)"
    return f"""\
DECLARE dup_keys INT64;
DECLARE n_deleted INT64;
DECLARE n_inserted INT64;

BEGIN TRANSACTION;

DELETE FROM `{t}` WHERE {scope};
SET n_deleted = @@row_count;

INSERT INTO `{t}` ({cols})
SELECT {cols} FROM `{load_table(project, dataset)}`;
SET n_inserted = @@row_count;

SET dup_keys = (
  SELECT COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT(left_id, right_id)))
  FROM `{t}`);
IF dup_keys > 0 THEN
  ROLLBACK TRANSACTION;
  RAISE USING MESSAGE = FORMAT('{t}: %d duplicate (left_id, right_id) pairs. Rolled back.', dup_keys);
END IF;

COMMIT TRANSACTION;

SELECT n_deleted, n_inserted;
"""


def check_sql(project: str, dataset: str) -> str:
    return f"""\
SELECT
  COUNT(*) AS links,
  COUNT(*) - COUNT(DISTINCT CONCAT(left_id, '|', right_id)) AS duplicates,
  COUNTIF(method IS NULL OR method != 'model') AS not_model,
  COUNTIF(score IS NULL OR score < 0 OR score > 1) AS bad_score
FROM `{target(project, dataset)}`"""


def model_label(model_version: str) -> str:
    """A BigQuery label value (lowercase letters, digits, - and _; at most 63 characters).
    Same rule as Model-Development/matching s13_build_candidates.model_label: change both together."""
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in model_version.lower())[:63]


def label_sql(project: str, dataset: str, model_version: str) -> str:
    """Stamp the link table with the bundle + gate that made its links (table metadata, no cost).
    s13 --since auto compares it with the next run's version: a difference means a full rescore."""
    return (f"ALTER TABLE `{target(project, dataset)}` "
            f"SET OPTIONS (labels = [('model_version', '{model_label(model_version)}')])")


def check_problems(counts: dict) -> list[str]:
    """What is wrong with the model link table, from its check_sql counts. Pure."""
    problems = []
    if counts["duplicates"]:
        problems.append(f"{counts['duplicates']} duplicate (left_id, right_id) pairs")
    if counts["not_model"]:
        problems.append(f"{counts['not_model']} rows with method other than 'model'")
    if counts["bad_score"]:
        problems.append(f"{counts['bad_score']} rows with a score outside [0, 1]")
    return problems


def run_metrics(report: dict, counts: dict, loaded: dict, dataset: str, full: bool,
                run_id: str | None, storage: dict | None = None) -> dict:
    """The fields of the ELK record matching_run_metrics. Pure."""
    return {**report, "dataset": dataset, "full": full, "run_id": run_id,
            "links_deleted": loaded["n_deleted"], "links_inserted": loaded["n_inserted"],
            "links_total": counts["links"], **(storage or {})}


def read_chunks(path: Path | None) -> list[dict]:
    """s12's chunks.jsonl (one JSON object per scored chunk), or [] without a file."""
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def dir_bytes(path: Path) -> int:
    """Bytes of every file under a run folder (candidates, checkpoints, links, report)."""
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def storage_sizes(client, project: str, dataset: str, run_dir: Path) -> dict:
    """Table sizes from table metadata (no query cost) and the run folder's size on disk."""
    links = client.get_table(target(project, dataset))
    cands = client.get_table(candidates_table(project, dataset))
    return {"link_table_bytes": int(links.num_bytes or 0), "link_table_rows": int(links.num_rows or 0),
            "candidates_table_bytes": int(cands.num_bytes or 0), "run_dir_bytes": dir_bytes(run_dir)}


# ── BigQuery ──────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--dataset", default="vigil_md_dev")
    ap.add_argument("--links", type=Path, help="s12 output (parquet)")
    ap.add_argument("--report", type=Path, help="s12 --report (JSON), sent to ELK")
    ap.add_argument("--chunks", type=Path, help="s12 chunks.jsonl: one ELK record per chunk")
    ap.add_argument("--full", action="store_true", help="replace every model link")
    ap.add_argument("--run-id", help="Airflow run_id, for the ELK record")
    ap.add_argument("--model-version",
                    help="bundle version + gate that made these links; written as the table label "
                         "model_version (s13 --since auto reads it)")
    ap.add_argument("--max-gb", type=float, default=MAX_GB)
    ap.add_argument("--print-only", action="store_true", help="print the SQL; no GCP call")
    a = ap.parse_args(argv)

    try:
        sqls = [create_sql(a.project, a.dataset), merge_sql(a.project, a.dataset, a.full),
                check_sql(a.project, a.dataset),
                view_sql(a.project, a.dataset, ORDER + [TABLE])]
    except ValueError as e:
        log.error("%s", e)
        return EXIT_BAD_INPUT
    if a.print_only:
        print(";\n\n".join(sqls) + ";")
        return 0
    if a.links is None or not a.links.exists():
        log.error("--links %s not found", a.links)
        return EXIT_BAD_INPUT
    create, merge, check, view = sqls

    from google.cloud import bigquery
    client = bigquery.Client(project=a.project)
    cap = int(a.max_gb * 1e9)

    client.query(create).result()
    schema = [bigquery.SchemaField(c, t) for c, t in schemas.LINK_COLUMNS]
    with open(a.links, "rb") as f:
        client.load_table_from_file(f, load_table(a.project, a.dataset), job_config=bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET, schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)).result()

    try:
        loaded = dict(next(iter(client.query(merge, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=cap)).result())))
    except Exception as e:                      # the RAISE inside the transaction
        if "duplicate" in str(e):
            log.error("%s", e)
            return EXIT_CHECK_FAILED
        raise
    counts = dict(next(iter(client.query(check, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=CHECK_MAX_BYTES)).result())))
    problems = check_problems(counts)
    if problems:
        log.error("%s failed its checks: %s", TABLE, "; ".join(problems))
        return EXIT_CHECK_FAILED
    client.query(view).result()
    if a.model_version:       # only after a clean load: a failed run keeps the old stamp
        client.query(label_sql(a.project, a.dataset, a.model_version)).result()

    report = json.loads(a.report.read_text()) if a.report else {}
    for chunk in read_chunks(a.chunks):
        log.info("matching_chunk_scored", extra={**chunk, "dataset": a.dataset, "run_id": a.run_id})
    storage = storage_sizes(client, a.project, a.dataset, a.links.parent)
    log.info("matching_run_metrics", extra={**run_metrics(report, counts, loaded, a.dataset, a.full,
                                                          a.run_id, storage),
                                            "model_version": a.model_version})
    print(f"=== {a.dataset}.{TABLE}: {loaded['n_deleted']:,} links deleted, "
          f"{loaded['n_inserted']:,} inserted, {counts['links']:,} in total; view links updated ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
