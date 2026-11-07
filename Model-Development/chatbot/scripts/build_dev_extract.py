"""Build the development dataset vigil_md_dev: a small copy of vigil_md_prod for a few devices.

Same table names and layout (partitioning, clustering) as vigil_md_prod, so the query layer
runs on it unchanged (VIGIL_BQ_DATASET=vigil_md_dev). What is copied is set in
data/dev_extract.json (product codes, start date):

  maude           reports for the product codes, received from from_date
  maude_problems  the problem terms of exactly those reports
  maude_text      the narratives of exactly those reports
  recall          all recalls for the product codes
  enforcement     the enforcement rows of those recalls
  gudid           device records that list one of the product codes

It only reads vigil_md_prod and only writes the target dataset, which must be a
vigil_md_dev* dataset. Each table is replaced in one CREATE OR REPLACE TABLE ... AS SELECT,
labelled with the extract version.

Usage (from Model-Development/chatbot/):
    python scripts/build_dev_extract.py --print-only       # the SQL, no GCP call
    python scripts/build_dev_extract.py --estimate-only    # dry runs (free): GB per table
    python scripts/build_dev_extract.py --yes              # build (reads about the estimate)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "data" / "dev_extract.json"
PROJECT = "vigil-md-1792696816"
LOCATION = "US"
TABLE_ORDER = ("maude", "maude_problems", "maude_text", "recall", "enforcement", "gudid")
LAYOUT = {   # same as vigil_md_prod (Data-Pipeline/scripts/warehouse/schemas.py)
    "maude":          ("DATE_TRUNC(_part_date, MONTH)", "source_record_id, device_seq"),
    "maude_problems": ("DATE_TRUNC(_part_date, MONTH)", "mdr_report_key"),
    "maude_text":     ("DATE_TRUNC(_part_date, MONTH)", "mdr_report_key"),
    "recall":         ("DATE_TRUNC(_part_date, MONTH)", "source_record_id"),
    "enforcement":    ("DATE_TRUNC(_part_date, MONTH)", "source_record_id"),
    "gudid":          (None, "source_record_id"),
}


def load_config(path: Path = CONFIG) -> dict:
    """Read and validate the extract spec (values are inlined into SQL, so they are checked)."""
    cfg = json.loads(path.read_text())
    codes = list(cfg["product_codes"])
    if not codes or not all(re.fullmatch(r"[A-Z]{3}", c) for c in codes):
        raise ValueError(f"product codes must be 3 capital letters: {codes}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg["from_date"]):
        raise ValueError(f"from_date must be YYYY-MM-DD: {cfg['from_date']}")
    if not re.fullmatch(r"vigil_md_dev\w*", cfg["target_dataset"]):
        raise ValueError(f"target must be a vigil_md_dev* dataset, got {cfg['target_dataset']}")
    if cfg["source_dataset"] == cfg["target_dataset"]:
        raise ValueError("source and target are the same dataset")
    if not isinstance(cfg["version"], int):
        raise ValueError("version must be an integer")
    return {**cfg, "codes": codes}


def select_sql(table: str, cfg: dict, project: str = PROJECT) -> str:
    """The SELECT that copies one table. Pure function."""
    src = f"`{project}.{cfg['source_dataset']}`"
    dst = f"`{project}.{cfg['target_dataset']}`"
    codes = ", ".join(f"'{c}'" for c in cfg["codes"])
    since = f"DATE '{cfg['from_date']}'"
    if table == "maude":
        return (f"SELECT * FROM {src}.maude\n"
                f"WHERE _part_date >= {since} AND product_code IN ({codes})")
    if table in ("maude_problems", "maude_text"):
        # the reports already copied to the target (maude is built first)
        return (f"SELECT * FROM {src}.{table}\n"
                f"WHERE _part_date >= {since}\n"
                f"  AND mdr_report_key IN (SELECT DISTINCT source_record_id FROM {dst}.maude)")
    if table == "recall":
        return f"SELECT * FROM {src}.recall\nWHERE product_code IN ({codes})"
    if table == "enforcement":
        return (f"SELECT * FROM {src}.enforcement\n"
                f"WHERE source_record_id IN (SELECT source_record_id FROM {dst}.recall)")
    if table == "gudid":
        return (f"SELECT * FROM {src}.gudid\n"
                f"WHERE EXISTS (SELECT 1 FROM UNNEST(product_codes) AS pc WHERE pc IN ({codes}))")
    raise ValueError(f"unknown table {table}")


def create_sql(table: str, cfg: dict, project: str = PROJECT) -> str:
    """CREATE OR REPLACE TABLE in the target, same layout as prod, labelled. Pure function."""
    partition, cluster = LAYOUT[table]
    target = f"`{project}.{cfg['target_dataset']}.{table}`"
    if target.startswith(f"`{project}.{cfg['source_dataset']}."):
        raise ValueError("refusing to write the source dataset")
    since = f" from {cfg['from_date']}" if table in ("maude", "maude_problems", "maude_text") else ""
    desc = (f"Development extract v{cfg['version']}: {table} for product codes "
            f"{', '.join(cfg['codes'])}{since}, copied from {cfg['source_dataset']}")
    parts = [f"CREATE OR REPLACE TABLE {target}"]
    if partition:
        parts.append(f"PARTITION BY {partition}")
    parts.append(f"CLUSTER BY {cluster}")
    parts.append(f"OPTIONS (description = '{desc}', "
                 f"labels = [('dev_extract_version', 'v{cfg['version']}'), "
                 f"('copied_from', '{cfg['source_dataset'].replace('_', '-')}')])")
    parts.append("AS\n" + select_sql(table, cfg, project))
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=CONFIG)
    ap.add_argument("--print-only", action="store_true", help="print the SQL; no GCP call")
    ap.add_argument("--estimate-only", action="store_true", help="dry runs only (free)")
    ap.add_argument("--yes", action="store_true", help="build the tables (writes the target)")
    ap.add_argument("--max-gb", type=float, default=30.0,
                    help="refuse to build if the dry runs add up to more than this")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    if a.print_only:
        for t in TABLE_ORDER:
            print(f"-- {t}\n{create_sql(t, cfg)};\n")
        return 0
    if not (a.estimate_only or a.yes):
        print("Nothing done: use --print-only, --estimate-only or --yes.")
        return 2

    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT)
    ds_id = f"{PROJECT}.{cfg['target_dataset']}"
    try:
        client.get_dataset(ds_id)
        exists = True
    except NotFound:
        exists = False

    # Tables after maude read the target's maude/recall: until those exist, estimate them
    # with the source-side SELECT (an upper bound for the prod tables they read).
    total = 0
    for t in TABLE_ORDER:
        sql = create_sql(t, cfg) if exists else select_sql(t, {**cfg, "target_dataset": cfg["source_dataset"]})
        try:
            b = client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True)).total_bytes_processed
        except NotFound:   # target exists but this table does not yet
            b = client.query(select_sql(t, {**cfg, "target_dataset": cfg["source_dataset"]}),
                             job_config=bigquery.QueryJobConfig(dry_run=True)).total_bytes_processed
        total += b
        print(f"  {t:15} {b / 1e9:7.3f} GB")
    print(f"  {'total':15} {total / 1e9:7.3f} GB  (cap --max-gb {a.max_gb})")
    if a.estimate_only:
        return 0
    if total / 1e9 > a.max_gb:
        print("REFUSING: the estimate is above --max-gb. Nothing was created.")
        return 3

    if not exists:
        ds = bigquery.Dataset(ds_id)
        ds.location = LOCATION
        ds.description = "Development extract of vigil_md_prod for the chatbot (see dev_extract.json)"
        ds.labels = {"purpose": "chatbot-dev"}
        client.create_dataset(ds)
        print(f"created dataset {ds_id}")
    cap = int(a.max_gb * 1e9)
    for t in TABLE_ORDER:
        job = client.query(create_sql(t, cfg), job_config=bigquery.QueryJobConfig(maximum_bytes_billed=cap))
        job.result()
        tbl = client.get_table(f"{ds_id}.{t}")
        print(f"  {t:15} {tbl.num_rows:>10,} rows  {tbl.num_bytes / 1e6:8.1f} MB  "
              f"(billed {(job.total_bytes_billed or 0) / 1e9:.3f} GB)")
    print(f"=== {ds_id} built: extract v{cfg['version']}, codes {', '.join(cfg['codes'])}, "
          f"from {cfg['from_date']} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
