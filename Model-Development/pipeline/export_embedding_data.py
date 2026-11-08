"""Export the data for the embedding prototype from BigQuery (read-only) to CSV.

Three read-only queries on vigil_md_prod:
  sql/embedding/01_labelers.sql       which GUDID labelers the prototype covers
  sql/embedding/02_gudid_groups.sql   every GUDID model group of those labelers (the search index)
  sql/embedding/03_maude_queries.sql  UDI-linked MAUDE tuples of those labelers (the known answers)

Outputs in data/embedding/<seed>/ (git-ignored; re-run the script to rebuild them):
  labelers.csv, gudid_groups.csv, maude_queries.csv, manifest.json

All three queries are dry-run first (free). If any is above --max-gb, nothing runs and the script
exits 3. Each real job also sets maximum_bytes_billed. If the folder already has data, the script
exits 4 unless --overwrite is given.

Usage (from Model-Development/):
  $PY scripts/export_embedding_data.py --project vigil-md-1792696816 --print-only
  $PY scripts/export_embedding_data.py --project vigil-md-1792696816 --estimate-only
  $PY scripts/export_embedding_data.py --project vigil-md-1792696816
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from vigil_model import bq_utils  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql" / "embedding"
OUT_ROOT = ROOT / "data" / "embedding"
EXIT_OK, EXIT_BAD_SQL, EXIT_OVER_BUDGET, EXIT_EXISTS = 0, 2, 3, 4
QUERY_IDS = ("01_labelers", "02_gudid_groups", "03_maude_queries")
DEFAULT_SEED = "vigil-emb-v1"
DRY_RUN_LABELERS = ["dryrun labeler"]   # the scan size does not depend on the list

log = logging.getLogger("export_embedding_data")


def load_sql(project: str) -> dict[str, str]:
    files = {qf.qid: qf for qf in bq_utils.load_query_dir(SQL_DIR)}
    missing = set(QUERY_IDS) - set(files)
    if missing:
        raise FileNotFoundError(f"missing SQL files in {SQL_DIR}: {sorted(missing)}")
    out = {}
    for qid in QUERY_IDS:
        qf = files[qid]
        if len(qf.parts) != 1:
            raise ValueError(f"{qf.path.name}: expected one query, found {len(qf.parts)} parts")
        sql = bq_utils.render(qf.parts[0][1], project)
        bq_utils.assert_read_only(sql)
        out[qid] = sql
    return out


def params_for(qid: str, args, labelers: list[str]):
    from google.cloud import bigquery
    S, A = bigquery.ScalarQueryParameter, bigquery.ArrayQueryParameter
    if qid == "01_labelers":
        return [S("seed", "STRING", args.seed), S("n_top", "INT64", args.n_top),
                S("n_mid", "INT64", args.n_mid), S("n_small", "INT64", args.n_small),
                S("max_dis_per_labeler", "INT64", args.max_dis_per_labeler),
                S("min_linked_rows", "INT64", args.min_linked_rows)]
    if qid == "02_gudid_groups":
        return [A("labelers", "STRING", labelers)]
    return [A("labelers", "STRING", labelers), S("seed", "STRING", args.seed),
            S("max_queries", "INT64", args.max_queries)]


def dry_run_bytes(client, sql: str, params) -> int:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=params))
    return int(job.total_bytes_processed or 0)


def run_query(client, sql: str, params, max_bytes: int) -> tuple[list[dict], int]:
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=max_bytes))
    rows = [dict(r.items()) for r in job.result()]
    return rows, int(job.total_bytes_processed or 0)


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: "" if v is None else v for k, v in r.items()})


def summarize(labelers: list[dict], groups: list[dict], queries: list[dict]) -> dict:
    """Counts that show at a glance whether the export is usable."""
    known = {g["group_key"] for g in groups}
    missing = sum(1 for q in queries
                  if not set(str(q["true_group_keys"]).split(";")) & known)
    return {
        "labelers": len(labelers),
        "labelers_by_tier": dict(Counter(l["tier"] for l in labelers)),
        "gudid_groups": len(groups),
        "gudid_dis": sum(int(g["n_dis"]) for g in groups),
        "queries": len(queries),
        "query_pool_tuples": int(queries[0]["pool_tuples"]) if queries else 0,
        "queries_id_agrees": sum(bool(q["id_agrees"]) for q in queries),
        "queries_mfr_equal": sum(bool(q["mfr_equal"]) for q in queries),
        "queries_has_identifier": sum(bool(q["has_identifier"]) for q in queries),
        "queries_answer_not_in_index": missing,     # must be 0
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True)
    p.add_argument("--seed", default=DEFAULT_SEED)
    p.add_argument("--n-top", type=int, default=5, help="labelers from the top 10 by linked rows")
    p.add_argument("--n-mid", type=int, default=15, help="labelers ranked 11-100")
    p.add_argument("--n-small", type=int, default=30, help="labelers ranked 101+")
    p.add_argument("--max-dis-per-labeler", type=int, default=100_000,
                   help="skip labelers with more DIs than this (keeps the index laptop-sized)")
    p.add_argument("--min-linked-rows", type=int, default=20)
    p.add_argument("--max-queries", type=int, default=20_000, help="MAUDE tuples to keep")
    p.add_argument("--max-gb", type=float, default=10.0, help="per-query scan limit")
    p.add_argument("--print-only", action="store_true", help="print the SQL, no GCP call")
    p.add_argument("--estimate-only", action="store_true", help="dry runs only (free)")
    p.add_argument("--overwrite", action="store_true")
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    try:
        sql = load_sql(args.project)
    except (ValueError, FileNotFoundError) as e:
        log.error("%s", e)
        return EXIT_BAD_SQL
    if args.print_only:
        print("\n\n".join(f"-- {qid}\n{s}" for qid, s in sql.items()))
        return EXIT_OK

    out_dir = OUT_ROOT / args.seed
    if not args.estimate_only and (out_dir / "manifest.json").exists() and not args.overwrite:
        log.error("%s already has an export. Use a new --seed or --overwrite.", out_dir)
        return EXIT_EXISTS

    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=args.project)
    max_bytes = int(args.max_gb * 1e9)

    est = {}
    for qid in QUERY_IDS:
        try:
            est[qid] = dry_run_bytes(client, sql[qid], params_for(qid, args, DRY_RUN_LABELERS))
        except Exception as e:  # a compile error comes back from the dry run
            log.error("dry run of %s failed: %s", qid, e)
            return EXIT_BAD_SQL
        log.info("%s: estimated %.2f GB", qid, est[qid] / 1e9)
    over = [q for q, b in est.items() if b > max_bytes]
    if over:
        log.error("over --max-gb %.1f: %s. Nothing was run.", args.max_gb, over)
        return EXIT_OVER_BUDGET
    if args.estimate_only:
        return EXIT_OK

    done = {}
    labelers, done["01_labelers"] = run_query(client, sql["01_labelers"],
                                              params_for("01_labelers", args, []), max_bytes)
    names = [l["mfr"] for l in labelers]
    log.info("labelers: %d %s", len(names), dict(Counter(l["tier"] for l in labelers)))
    if not names:
        log.error("no labeler matched the filters; nothing else was run")
        return EXIT_BAD_SQL
    groups, done["02_gudid_groups"] = run_query(client, sql["02_gudid_groups"],
                                                params_for("02_gudid_groups", args, names), max_bytes)
    log.info("gudid groups: %d", len(groups))
    queries, done["03_maude_queries"] = run_query(client, sql["03_maude_queries"],
                                                  params_for("03_maude_queries", args, names), max_bytes)
    log.info("maude queries: %d", len(queries))

    write_csv(out_dir / "labelers.csv", labelers)
    write_csv(out_dir / "gudid_groups.csv", groups)
    write_csv(out_dir / "maude_queries.csv", queries)
    manifest = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "project": args.project, "seed": args.seed,
        "params": {k: getattr(args, k) for k in ("n_top", "n_mid", "n_small",
                                                 "max_dis_per_labeler", "min_linked_rows",
                                                 "max_queries")},
        "bytes_processed": done,
        **summarize(labelers, groups, queries),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if manifest["queries_answer_not_in_index"]:
        log.warning("%d queries have no answer in the index; check the group_key logic",
                    manifest["queries_answer_not_in_index"])
    log.info("wrote %s", out_dir)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
