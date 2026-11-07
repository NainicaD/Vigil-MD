"""Draw the hand-labeled test set and write the labeling sheets.

Two read-only queries on vigil_md_prod:
  sql/test_set/01_sample.sql      stratified sample of MAUDE tuples from the no-UDI pool
  sql/test_set/02_candidates.sql  candidate GUDID model groups for each sampled tuple

Outputs in reports/test_set/<seed>/ (the seed is in the path, so a new seed never overwrites
a sheet that is being labeled):
  sample.csv          one row per sampled tuple, with stratum, weight and the row-level label columns
  labeling_sheet.csv  one row per (tuple, candidate group), with the pair label columns
  manifest.json       parameters, bytes scanned, counts per stratum

If the seed's folder already has a labeling sheet, the script exits 4 and writes nothing
(--overwrite replaces it). Both queries are dry-run first (free). If either is above --max-gb, nothing runs and the script
exits 3. Each real job also sets maximum_bytes_billed. A seed and the same prod data give the
same sample.

Usage (from Model-Development/, in WSL):
  $PY scripts/build_test_set.py --project vigil-md-1792696816 --print-only
  $PY scripts/build_test_set.py --project vigil-md-1792696816 --estimate-only
  $PY scripts/build_test_set.py --project vigil-md-1792696816 --per-stratum 35
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bq_utils  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql" / "test_set"
OUT_ROOT = ROOT / "reports" / "test_set"
EXIT_OK, EXIT_BAD_SQL, EXIT_OVER_BUDGET, EXIT_EXISTS = 0, 2, 3, 4

DEFAULT_SEED = "vigil-test-v1"
DEFAULT_CAPS = {"cap_id_exact": 10, "cap_other": 5, "cap_name_any": 3, "cap_random": 2}
SAMPLE_KEY_FIELDS = ("sample_id", "mfr", "model_key", "catalog_key", "name_key")

# Label columns the labelers fill in. Allowed values are in docs/LABELING_GUIDE.md.
PAIR_LABEL_COLS = ("label", "edge_case", "labeler", "label_2", "labeler_2", "notes")
SAMPLE_LABEL_COLS = ("row_outcome", "labeler", "row_outcome_2", "labeler_2", "notes")

log = logging.getLogger("build_test_set")


# ---------------------------------------------------------------------------- queries

def load_queries() -> dict[str, bq_utils.QueryFile]:
    files = {qf.qid: qf for qf in bq_utils.load_query_dir(SQL_DIR)}
    missing = {"01_sample", "02_candidates"} - set(files)
    if missing:
        raise FileNotFoundError(f"missing SQL files in {SQL_DIR}: {sorted(missing)}")
    return files


def rendered_sql(qf: bq_utils.QueryFile, project: str) -> str:
    if len(qf.parts) != 1:
        raise ValueError(f"{qf.path.name}: expected one query, found {len(qf.parts)} parts")
    sql = bq_utils.render(qf.parts[0][1], project)
    bq_utils.assert_read_only(sql)
    return sql


def sample_params(per_stratum: int, seed: str):
    from google.cloud import bigquery
    return [bigquery.ScalarQueryParameter("per_stratum", "INT64", per_stratum),
            bigquery.ScalarQueryParameter("seed", "STRING", seed)]


def candidate_params(sample_rows: list[dict], seed: str, caps: dict[str, int]):
    from google.cloud import bigquery
    structs = [
        bigquery.StructQueryParameter(None, *[
            bigquery.ScalarQueryParameter(f, "STRING", row.get(f)) for f in SAMPLE_KEY_FIELDS])
        for row in sample_rows
    ]
    params = [bigquery.ArrayQueryParameter("sample", "STRUCT", structs),
              bigquery.ScalarQueryParameter("seed", "STRING", seed)]
    params += [bigquery.ScalarQueryParameter(k, "INT64", v) for k, v in caps.items()]
    return params


# A stand-in sample for the dry run of 02 before 01 has run. The scan size does not depend on it.
DRY_RUN_SAMPLE = [{"sample_id": "dryrun", "mfr": "x", "model_key": "X1",
                   "catalog_key": None, "name_key": None}]


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


# ---------------------------------------------------------------------------- sheets

def add_weights(sample_rows: list[dict]) -> list[dict]:
    """Add n_drawn and weight_tuple (= tuples in the stratum / tuples drawn from it).

    A metric over the pool's tuples is sum(w * y) / sum(w). A metric over the pool's MAUDE rows
    is sum(w * n_rows * y) / sum(w * n_rows). Rows are not equal-probability: a tuple with many
    rows was as likely to be drawn as a tuple with one.
    """
    drawn = Counter(r["stratum"] for r in sample_rows)
    out = []
    for r in sample_rows:
        r = dict(r)
        r["n_drawn"] = drawn[r["stratum"]]
        r["weight_tuple"] = round(int(r["stratum_tuples"]) / drawn[r["stratum"]], 6)
        out.append(r)
    return out


def double_label(sample_id: str, seed: str, share: float) -> bool:
    """Same answer for the same seed and id, whatever order the rows come in."""
    h = hashlib.sha256(f"{seed}|double|{sample_id}".encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF < share


def build_sample_sheet(sample_rows: list[dict], cand_rows: list[dict], seed: str,
                       share: float) -> list[dict]:
    n_cands = Counter(c["sample_id"] for c in cand_rows)
    out = []
    for r in add_weights(sample_rows):
        r["n_candidates"] = n_cands[r["sample_id"]]
        r["double_label"] = double_label(r["sample_id"], seed, share)
        for c in SAMPLE_LABEL_COLS:
            r[c] = ""
        out.append(r)
    return out


MAUDE_COLS = ("manufacturer_name_raw", "device_name_raw", "model_number_raw", "catalog_number",
              "product_code", "example_mdr_report_key", "example_device_seq", "n_rows")
GUDID_COLS = ("group_key", "sources", "best_rank", "n_dis", "rep_udi_di",
              "gudid_manufacturer_name_raw", "gudid_device_name_raw", "gudid_model_number_raw",
              "gudid_catalog_number", "gudid_catalog_examples", "gudid_product_codes",
              "gudid_device_record_status")


def build_pair_sheet(sample_sheet: list[dict], cand_rows: list[dict]) -> list[dict]:
    """One row per (tuple, candidate). A tuple with no candidate still gets one row, with the
    GUDID columns empty, so the labeler sees it and records a row outcome."""
    by_sample: dict[str, list[dict]] = {}
    for c in cand_rows:
        by_sample.setdefault(c["sample_id"], []).append(c)
    out = []
    for s in sample_sheet:
        left = {"sample_id": s["sample_id"], "stratum": s["stratum"],
                "double_label": s["double_label"], **{k: s.get(k) for k in MAUDE_COLS}}
        cands = sorted(by_sample.get(s["sample_id"], []),
                       key=lambda c: (int(c.get("best_rank") or 0), str(c.get("group_key"))))
        for c in cands or [{}]:
            row = dict(left)
            row.update({k: c.get(k, "") for k in GUDID_COLS})
            for col in PAIR_LABEL_COLS:
                row[col] = ""
            out.append(row)
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: "" if v is None else v for k, v in r.items()})


def summarize(sample_sheet: list[dict], cand_rows: list[dict]) -> dict:
    per_stratum: dict[str, dict] = {}
    for r in sample_sheet:
        st = per_stratum.setdefault(r["stratum"], {
            "pool_tuples": int(r["stratum_tuples"]), "pool_rows": int(r["stratum_rows"]),
            "drawn": 0, "no_candidates": 0, "double_label": 0})
        st["drawn"] += 1
        st["no_candidates"] += int(r["n_candidates"] == 0)
        st["double_label"] += int(bool(r["double_label"]))
    sources = Counter(s for c in cand_rows for s in str(c.get("sources", "")).split(";") if s)
    return {"tuples": len(sample_sheet), "pairs": len(cand_rows),
            "strata": dict(sorted(per_stratum.items())), "pairs_by_source": dict(sources)}


# ---------------------------------------------------------------------------- main

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--project", required=True)
    p.add_argument("--per-stratum", type=int, default=35,
                   help="tuples drawn per stratum (12 strata; default 35 -> about 420)")
    p.add_argument("--seed", default=DEFAULT_SEED)
    p.add_argument("--double-label-share", type=float, default=0.2,
                   help="share of tuples the second labeler also labels")
    p.add_argument("--max-gb", type=float, default=10.0, help="per-query scan limit")
    p.add_argument("--print-only", action="store_true", help="print the SQL, no GCP call")
    p.add_argument("--estimate-only", action="store_true", help="dry runs only (free)")
    p.add_argument("--overwrite", action="store_true",
                   help="replace an existing sheet for this seed (only if labeling has not started)")
    for k, v in DEFAULT_CAPS.items():
        p.add_argument(f"--{k.replace('_', '-')}", type=int, default=v)
    return p.parse_args(argv)


def main(argv=None, client=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse_args(argv)
    caps = {k: getattr(args, k) for k in DEFAULT_CAPS}
    try:
        files = load_queries()
        sql_sample = rendered_sql(files["01_sample"], args.project)
        sql_cands = rendered_sql(files["02_candidates"], args.project)
    except (ValueError, FileNotFoundError) as e:
        log.error("%s", e)
        return EXIT_BAD_SQL

    if args.print_only:
        print(f"-- 01_sample\n{sql_sample}\n\n-- 02_candidates\n{sql_cands}")
        return EXIT_OK

    out_dir = OUT_ROOT / args.seed
    if not args.estimate_only and (out_dir / "labeling_sheet.csv").exists() and not args.overwrite:
        log.error("%s already has a labeling sheet. Use a new --seed, or --overwrite if no one "
                  "has started labeling it.", out_dir)
        return EXIT_EXISTS

    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=args.project)
    max_bytes = int(args.max_gb * 1e9)

    est = {}
    for name, sql, params in (
            ("01_sample", sql_sample, sample_params(args.per_stratum, args.seed)),
            ("02_candidates", sql_cands, candidate_params(DRY_RUN_SAMPLE, args.seed, caps))):
        try:
            est[name] = dry_run_bytes(client, sql, params)
        except Exception as e:  # a compile error comes back from the dry run
            log.error("dry run of %s failed: %s", name, e)
            return EXIT_BAD_SQL
    for name, b in est.items():
        log.info("%s: estimated %.2f GB", name, b / 1e9)
    over = [n for n, b in est.items() if b > max_bytes]
    if over:
        log.error("over --max-gb %.1f: %s. Nothing was run.", args.max_gb, over)
        return EXIT_OVER_BUDGET
    if args.estimate_only:
        return EXIT_OK

    sample_rows, b1 = run_query(client, sql_sample, sample_params(args.per_stratum, args.seed),
                                max_bytes)
    log.info("sample: %d tuples, %.2f GB", len(sample_rows), b1 / 1e9)
    cand_rows, b2 = run_query(client, sql_cands, candidate_params(sample_rows, args.seed, caps),
                              max_bytes)
    log.info("candidates: %d pairs, %.2f GB", len(cand_rows), b2 / 1e9)

    sample_sheet = build_sample_sheet(sample_rows, cand_rows, args.seed, args.double_label_share)
    pair_sheet = build_pair_sheet(sample_sheet, cand_rows)
    write_csv(out_dir / "sample.csv", sample_sheet)
    write_csv(out_dir / "labeling_sheet.csv", pair_sheet)
    manifest = {
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "project": args.project, "seed": args.seed, "per_stratum": args.per_stratum,
        "double_label_share": args.double_label_share, "caps": caps,
        "bytes_processed": {"01_sample": b1, "02_candidates": b2},
        **summarize(sample_sheet, cand_rows),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("wrote %s", out_dir)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
