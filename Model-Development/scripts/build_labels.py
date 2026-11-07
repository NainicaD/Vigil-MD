"""Build the label set in BigQuery: run sql/labels/*.sql in order, write tables to vigil_md_model.

Reads vigil_md_prod and vigil_md_model, writes only vigil_md_model (every statement is one
CREATE OR REPLACE TABLE, so a re-run gives the same result). Rules reach the SQL as {{ macros }} from
same_device_features.py. Writes labels/label_set_v1.manifest.json after a real run.

Every part is dry-run before its job. Parts whose inputs are built earlier in the same run cannot be
dry-run until those tables exist, so they are estimated just before they run (still before any bytes
are billed for that part). A part over --max-gb stops the run with exit 3; maximum_bytes_billed is
set on every real job. Exit 0 ok, 2 broken SQL or config, 3 over budget, 4 golden fixture differs.

Train-only alias invariant: steps 02 and later refuse to run unless vigil_md_model.alias_table hashes
to the value in the manifest (written by --export-alias, which also writes labels/alias_table_v1.csv).

Usage (WSL; only whoever owns the write access runs it):
  ~/.venvs/vigil-analysis/bin/python Model-Development/scripts/build_labels.py \
      --project vigil-md-1792696816 --print-only
  ... --estimate-only
  ... --only 02 03 --snapshot-date 2026-11-06
  ... --export-alias                      # after 01_alias_table, before 02
  ... --fixture                           # Python / BigQuery parity check (H4), no tables read
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bq_utils  # noqa: E402
import golden_fixture  # noqa: E402
import same_device_features as feat  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql" / "labels"
LABELS_DIR = ROOT / "labels"
MANIFEST = LABELS_DIR / "label_set_v1.manifest.json"
ALIAS_CSV = LABELS_DIR / "alias_table_v1.csv"
MODEL_DATASET = bq_utils.MODEL_DATASET
EXIT_OK, EXIT_BAD_SQL, EXIT_OVER_BUDGET = 0, 2, 3
ALIAS_GATED_FROM = "02"          # file prefixes at or after this need the alias check

log = logging.getLogger("build_labels")


# --- rendering ----------------------------------------------------------------------------------
# The macro pass and the write guard live in bq_utils (N1). These names stay for callers and tests.
assert_model_write_only = bq_utils.assert_model_write_only


def expand_macros(sql: str) -> str:
    """{{ name(args) }} -> SQL, through same_device_features.SQL_MACROS. Unknown name or arity raises."""
    return bq_utils.expand_macros(sql, feat.SQL_MACROS)


def render_part(sql: str, project: str, params: dict[str, int]) -> str:
    sql = sql.replace("__MODEL_DATASET__", f"{project}.{MODEL_DATASET}")
    for name, value in params.items():
        sql = sql.replace(name, str(value))
    final = bq_utils.render(sql, project, macros=feat.SQL_MACROS)   # raises on any placeholder left over
    assert_model_write_only(final)
    return final


def rendered(files, project: str, params: dict[str, int]):
    for qf in files:
        for part, sql in qf.parts:
            yield qf, part, render_part(sql, project, params)


def target_of(sql: str) -> str:
    return re.search(r"CREATE OR REPLACE TABLE `([^`]+)`", sql).group(1)


def inputs_of(sql: str) -> set[str]:
    """vigil_md_model tables a part reads (its own target excluded)."""
    tables = set(re.findall(r"`([^`]+\.%s\.[^`]+)`" % MODEL_DATASET, re.sub(r"--[^\n]*", "", sql)))
    return tables - {target_of(sql)}


# --- alias table gate ---------------------------------------------------------------------------
def alias_csv_bytes(rows: list[dict]) -> bytes:
    """Canonical export: columns in table order, rows sorted, LF line ends. Hashed, committed, compared."""
    if not rows:
        raise ValueError("alias_table is empty")
    cols = list(rows[0])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in sorted(rows, key=lambda r: [str(r[c]) for c in cols]):
        w.writerow(["" if r[c] is None else r[c] for c in cols])
    return buf.getvalue().encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_alias_table(client, project: str) -> bytes:
    from google.cloud import bigquery
    sql = f"SELECT * FROM `{project}.{MODEL_DATASET}.alias_table`"
    rows = [dict(r.items()) for r in client.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=int(1e9))).result()]
    return alias_csv_bytes(rows)


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def export_alias(client, project: str, csv_path: Path, manifest_path: Path) -> int:
    data = read_alias_table(client, project)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_bytes(data)
    manifest = load_manifest(manifest_path)
    manifest["alias_table_sha256"] = sha256_hex(data)
    save_manifest(manifest_path, manifest)
    log.info("wrote %s (sha256 %s)", csv_path, manifest["alias_table_sha256"])
    return EXIT_OK


def check_alias(client, project: str, csv_path: Path, manifest_path: Path) -> str | None:
    """None when the BigQuery alias table is the one the manifest names; else why not."""
    want = load_manifest(manifest_path).get("alias_table_sha256")
    if not want:
        return f"{manifest_path.name} has no alias_table_sha256: run --export-alias first"
    if csv_path.exists() and sha256_hex(csv_path.read_bytes()) != want:
        return f"{csv_path.name} does not match the manifest hash: re-export or restore it"
    try:
        have = sha256_hex(read_alias_table(client, project))
    except Exception as e:
        return f"cannot read {MODEL_DATASET}.alias_table: {getattr(e, 'message', None) or e}"
    if have != want:
        return f"alias_table in BigQuery ({have[:12]}) differs from the manifest ({want[:12]}): re-export it"
    return None


# --- estimate and run ---------------------------------------------------------------------------
def _exists(client, table: str) -> bool:
    try:
        client.get_table(table)
        return True
    except Exception as e:
        if type(e).__name__ == "NotFound" or "Not found" in str(e):
            return False
        raise


def estimate_part(client, key, sql, max_gb: float, estimates: dict, failed: dict, over: dict) -> None:
    try:
        estimates[key] = bq_utils.estimate_bytes(client, sql)
    except Exception as e:   # report every broken part, not just the first
        failed[key] = getattr(e, "message", None) or str(e)
        log.error("FAIL %s/%s: %s", *key, failed[key])
        return
    log.info("estimate %s/%s: %.3f GB", *key, estimates[key] / 1e9)
    if estimates[key] / 1e9 > max_gb:
        over[key] = estimates[key]
        log.error("STOP %s/%s: %.3f GB is above --max-gb %.1f", *key, estimates[key] / 1e9, max_gb)


def run(client, project: str, files, params: dict[str, int], max_gb: float, estimate_only: bool = False,
        snapshot_date: str | None = None, manifest_path: Path = MANIFEST, alias_csv: Path = ALIAS_CSV) -> int:
    from google.cloud import bigquery

    items = list(rendered(files, project, params))
    if not estimate_only and not snapshot_date:
        log.error("--snapshot-date is required for a real run (it goes in the manifest)")
        return EXIT_BAD_SQL
    if any(qf.qid[:2] >= ALIAS_GATED_FROM for qf, _, _ in items):
        why = check_alias(client, project, alias_csv, manifest_path)
        if why:
            log.error("refusing to build labels: %s", why)
            return EXIT_BAD_SQL

    estimates, failed, over, deferred, made = {}, {}, {}, {}, set()
    for qf, part, sql in items:
        key = (qf.qid, part)
        needs = inputs_of(sql)
        if needs & made:
            deferred[key] = sorted(needs & made)
        else:
            missing = [t for t in sorted(needs) if not _exists(client, t)]
            if missing:
                failed[key] = f"needs {missing}, which no earlier selected step builds"
                log.error("FAIL %s/%s: %s", *key, failed[key])
            else:
                estimate_part(client, key, sql, max_gb, estimates, failed, over)
        made.add(target_of(sql))
    log.info("estimated %.3f GB now; %d parts wait for tables built earlier in this run",
             sum(estimates.values()) / 1e9, len(deferred))
    if failed:
        return EXIT_BAD_SQL
    if over:
        return EXIT_OVER_BUDGET
    if estimate_only:
        return EXIT_OK

    for qf, part, sql in items:
        key = (qf.qid, part)
        if key in deferred:
            estimate_part(client, key, sql, max_gb, estimates, failed, over)
            if failed:
                return EXIT_BAD_SQL
            if over:
                return EXIT_OVER_BUDGET
        job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=int(max_gb * 1e9)))
        job.result()
        log.info("built %s/%s -> %s (%s bytes processed)", *key, target_of(sql),
                 getattr(job, "total_bytes_processed", None))

    write_manifest(client, project, params, max_gb, snapshot_date, manifest_path,
                   with_counts=any(target_of(sql).endswith(".label_stats_counts") for _, _, sql in items))
    return EXIT_OK


def write_manifest(client, project, params, max_gb, snapshot_date, path: Path, with_counts: bool) -> None:
    from google.cloud import bigquery
    manifest = load_manifest(path)          # keeps alias_table_sha256 written by --export-alias
    all_files = sorted(SQL_DIR.glob("*.sql"))
    manifest.update({
        "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "snapshot_date": snapshot_date,
        "project": project,
        "query_sha256": {p.name: sha256_hex(p.read_bytes()) for p in all_files},
        "constants": feat.constants(),
        "constants_sha256": feat.constants_sha256(),
        "caps_and_ratios": {k.strip("_").lower(): v for k, v in params.items()} | {"max_gb": max_gb},
    })
    if with_counts:
        sql = (f"SELECT meta_split, label, key_match_kind, SUM(n_rows) AS n_rows "
               f"FROM `{project}.{MODEL_DATASET}.label_stats_counts` GROUP BY 1, 2, 3")
        rows = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=int(1e9))).result()
        manifest["row_counts"] = {f"{r['meta_split']}|{r['label']}|{r['key_match_kind']}": int(r["n_rows"])
                                  for r in rows}
    save_manifest(path, manifest)
    log.info("wrote %s", path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--sql-dir", type=Path, default=SQL_DIR)
    ap.add_argument("--only", nargs="*", help="file prefixes to run, e.g. 02 03")
    ap.add_argument("--max-gb", type=float, default=25.0,
                    help="stop if a part is estimated above this (the MAUDE x GUDID join is heavy)")
    ap.add_argument("--estimate-only", action="store_true", help="dry-run only, print costs")
    ap.add_argument("--print-only", action="store_true", help="print the SQL; no GCP call")
    ap.add_argument("--export-alias", action="store_true",
                    help="write labels/alias_table_v1.csv from BigQuery and record its hash in the manifest")
    ap.add_argument("--fixture", action="store_true",
                    help="run the golden fixture on BigQuery and compare with the expected values "
                         "(reads no table; exit 4 on any difference)")
    ap.add_argument("--snapshot-date", help="prod snapshot date for the manifest, e.g. 2026-11-06")
    ap.add_argument("--max-per-device", type=int, default=200, help="positives kept per device")
    ap.add_argument("--neg-per-anchor", type=int, default=2, help="negatives kept per anchor")
    ap.add_argument("--cand-pool", type=int, default=50, help="models kept per labeler and product code")
    ap.add_argument("--easy-anchor-pct", type=int, default=10, help="percent of positives given an easy negative")
    ap.add_argument("--easy-pool", type=int, default=20000, help="GUDID records per split for easy negatives")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.fixture:
        if args.print_only:
            print(golden_fixture.fixture_sql(golden_fixture.load()[1]))
            return EXIT_OK
        from google.cloud import bigquery
        return golden_fixture.check_bigquery(bigquery.Client(project=args.project))

    params ={"__MAX_PER_DEVICE__": args.max_per_device, "__NEG_PER_ANCHOR__": args.neg_per_anchor,
              "__CAND_POOL__": args.cand_pool, "__EASY_ANCHOR_PCT__": args.easy_anchor_pct,
              "__EASY_POOL__": args.easy_pool}
    if min(params.values()) < 1 or args.easy_anchor_pct > 100:
        log.error("caps must be positive and --easy-anchor-pct at most 100")
        return EXIT_BAD_SQL
    try:
        files = bq_utils.load_query_dir(args.sql_dir, args.only)
        if args.print_only:
            for qf, part, sql in rendered(files, args.project, params):
                print(f"-- ===== {qf.qid} / {part} =====\n{sql}\n")
            return EXIT_OK
        items_ok = list(rendered(files, args.project, params))   # fail on bad SQL before any GCP call
    except (ValueError, FileNotFoundError) as e:
        log.error("%s", e)
        return EXIT_BAD_SQL
    del items_ok

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    if args.export_alias:
        return export_alias(client, args.project, ALIAS_CSV, MANIFEST)
    return run(client, args.project, files, params, args.max_gb, args.estimate_only, args.snapshot_date)


if __name__ == "__main__":
    sys.exit(main())
