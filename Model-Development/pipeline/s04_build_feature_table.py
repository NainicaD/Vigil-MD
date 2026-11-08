"""Build the training feature table: label_set_v1 (key_* columns) -> feat_* columns.

label_set_v1 carries the raw inputs, never features, so the UDI has no path into a
feature. This stage calls same_device_features.pair_features on every row -- the same
function pipeline/s05 runs on the hand sheet, so training and evaluation features
cannot drift apart.

Everything it needs is in the parquet except sibling_keys, which the label set does not
carry (docs/LABELS.md section 5 item 7). Without it feat_conflicting_identifier is
always False and the 23,197 suffix_sibling negatives teach the model nothing. One small
query fills that gap: it asks BigQuery which MAUDE keys actually exist in GUDID under
the candidate's labeler, and returns only those, rather than downloading every GUDID key.

A second query ranks MAUDE manufacturers by report volume, which gives every row a
meta_mfr_tier for slicing metrics (the bias report found the top 10 under-represented in
training). Both answers are cached, so only the first build costs anything.

Usage (from anywhere):
    python pipeline/s04_build_feature_table.py --project <id> --print-only
    python pipeline/s04_build_feature_table.py --project <id> --estimate-only
    python pipeline/s04_build_feature_table.py --project <id> \
        --gudid-dataset vigil_md_dev --limit 5000          # cheap smoke test
    python pipeline/s04_build_feature_table.py --project <id>
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model import bq_utils  # noqa: E402
from vigil_model import same_device_features as feat  # noqa: E402
from vigil_model.rows import load_aliases, number, product_codes, text  # noqa: E402
from vigil_model.same_device_features import (  # noqa: E402
    FEATURE_COLUMNS, DeviceSide, MatchContext, assert_no_leakage, assert_no_value_leakage,
    pair_features,
)

log = logging.getLogger("build_feature_table")
EXIT_OK, EXIT_BAD_SQL, EXIT_OVER_BUDGET = 0, 2, 3
LEAK_SAMPLE = 2000          # rows value-checked for a leaked DI; the check is O(columns) per row

# Columns carried through for training, splitting and slicing. No identifier text and no
# DI: assert_no_leakage rejects those names, and the model must not see them.
CARRY = ["pair_id", "label", "label_source", "meta_split", "meta_device_key", "meta_year",
         "key_match_kind"]
# meta_, so it is never a feature: it is for slicing metrics, and the model must not learn
# "this labeler is big". Boundaries match TIERS in Data-Pipeline/scripts/analysis/bias_slices.py
# and mfr_size in sql/same_device/14_no_udi_pool.sql, so the bias report and the model's
# per-tier numbers name the same groups.
TIERS = (10, 100)
META = CARRY + ["meta_mfr_tier"]

# Which MAUDE keys exist in GUDID under the candidate's labeler. key_g_mfr is the right
# side: it is what sql/hand_labels/03_candidates.sql joins the sibling table on.
SIBLING_SQL = """
WITH gkeys AS (
  SELECT DISTINCT manufacturer_name_norm AS mfr, k AS sib_key
  FROM `{gudid}`, UNNEST([{{{{ usable_key(model_number_raw) }}}},
                          {{{{ usable_key(catalog_number) }}}}]) AS k
  WHERE k IS NOT NULL AND manufacturer_name_norm IS NOT NULL
),
needed AS (
  SELECT DISTINCT key_g_mfr AS mfr, k AS sib_key
  FROM `{label_set}`, UNNEST([{{{{ usable_key(key_m_model_raw) }}}},
                              {{{{ usable_key(key_m_catalog_raw) }}}}]) AS k
  WHERE k IS NOT NULL AND key_g_mfr IS NOT NULL
)
SELECT n.mfr, n.sib_key FROM needed AS n JOIN gkeys AS g USING (mfr, sib_key)
"""

# Report volume per MAUDE manufacturer. Ranked over all of MAUDE, not over the label set:
# the label set is the biased sample, so ranking inside it would hide the bias it measures.
# Only the first 100 names come back; everything else is long_tail by definition.
MFR_RANK_SQL = """
SELECT manufacturer_name_norm AS mfr,
       ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, manufacturer_name_norm) AS rk
FROM `{maude}`
WHERE manufacturer_name_norm IS NOT NULL
GROUP BY manufacturer_name_norm
QUALIFY ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, manufacturer_name_norm) <= {top}
ORDER BY rk
"""


def assert_select_datasets(sql: str, allowed: set[str]) -> None:
    """Raise unless the SQL is one SELECT reading only `allowed`.

    A separate guard on purpose, like the three in bq_utils: those pin vigil_md_prod and
    vigil_md_model as constants and must stay strict, but this stage may also read a dev
    copy of GUDID. bq_utils is untouched.
    """
    code = re.sub(r"--[^\n]*", "", sql).strip()
    head = code.split(None, 1)[0].upper() if code else ""
    if head not in ("SELECT", "WITH", "("):
        raise ValueError(f"not a SELECT: {code[:60]!r}")
    hit = bq_utils.READ_ONLY_FORBIDDEN.search(code)
    if hit:
        raise ValueError(f"write statement in a read query: {hit.group(0)}")
    datasets = set(re.findall(r"`[^`.]+\.([^`.]+)\.[^`]+`", code))
    if not datasets or not datasets <= allowed:
        raise ValueError(f"must read only {sorted(allowed)}, found {sorted(datasets)}")


def sibling_sql(project: str, gudid_dataset: str, table: str) -> str:
    sql = SIBLING_SQL.format(gudid=f"{project}.{gudid_dataset}.gudid",
                             label_set=f"{project}.{bq_utils.MODEL_DATASET}.{table}")
    sql = bq_utils.expand_macros(sql, feat.SQL_MACROS)
    assert_select_datasets(sql, {gudid_dataset, bq_utils.MODEL_DATASET})
    return sql


def mfr_rank_sql(project: str, dataset: str) -> str:
    sql = MFR_RANK_SQL.format(maude=f"{project}.{dataset}.maude", top=TIERS[1])
    assert_select_datasets(sql, {dataset})
    return sql


def read_siblings(path: Path) -> dict[str, frozenset[str]]:
    """mfr -> the keys that exist in GUDID for it, from the cached CSV."""
    out: dict[str, set[str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["mfr"], set()).add(row["sib_key"])
    return {k: frozenset(v) for k, v in out.items()}


def read_mfr_rank(path: Path) -> dict[str, int]:
    """mfr -> its rank by MAUDE report count, from the cached CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        return {row["mfr"]: int(row["rk"]) for row in csv.DictReader(f)}


def fetch_rows(client, sql: str, max_bytes: int, out: Path, cols: tuple[str, ...]) -> int:
    """Run a query, cache the result as CSV, return the bytes it billed."""
    from google.cloud import bigquery
    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes))
    rows = [tuple(r[c] for c in cols) for r in job.result()]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    log.info("%d rows -> %s", len(rows), out)
    return int(job.total_bytes_processed or 0)


def tier_of(mfr: str | None, ranks: dict[str, int]) -> str:
    """Which manufacturer-volume tier a MAUDE row belongs to. Absent from ranks = outside the top 100."""
    if not mfr:
        return "mfr_missing"
    rk = ranks.get(mfr)
    if rk is None:
        return "long_tail"
    return f"top_{TIERS[0]}" if rk <= TIERS[0] else f"{TIERS[0] + 1}_{TIERS[1]}"


def device_sides(r: dict) -> tuple[DeviceSide, DeviceSide]:
    """The two sides of one label_set_v1 row."""
    maude = DeviceSide(
        manufacturer_norm=text(r["key_m_mfr"]),
        device_name=text(r["key_m_name"]),
        model_raw=text(r["key_m_model_raw"]),
        catalog_raw=text(r["key_m_catalog_raw"]),
        product_codes=product_codes(r["key_m_pc"]),
    )
    gudid = DeviceSide(
        manufacturer_norm=text(r["key_g_mfr"]),
        device_name=text(r["key_g_name"]),
        model_raw=text(r["key_g_model_raw"]),
        catalog_raw=text(r["key_g_catalog_raw"]),
        product_codes=product_codes(r["key_g_pcs"]),
    )
    return maude, gudid


def build(data: pd.DataFrame, aliases, siblings: dict[str, frozenset[str]],
          ranks: dict[str, int]) -> pd.DataFrame:
    """One feat_* row per input row, in input order."""
    assert_no_leakage(FEATURE_COLUMNS)
    rows = []
    for i, r in enumerate(data.to_dict("records")):
        maude, gudid = device_sides(r)
        ctx = MatchContext(alias_pairs=aliases,
                           n_dis_for_model=number(r["meta_n_dis_for_model"]),
                           sibling_keys=siblings.get(text(r["key_g_mfr"]) or "", frozenset()))
        feats = pair_features(maude, gudid, ctx)
        if i < LEAK_SAMPLE:
            assert_no_value_leakage(feats, text(r.get("key_gudid_udi_di")))
        rows.append({**{c: r[c] for c in CARRY},
                     "meta_mfr_tier": tier_of(text(r["key_m_mfr"]), ranks), **feats})
    out = pd.DataFrame(rows, columns=META + list(FEATURE_COLUMNS))
    assert_no_leakage([c for c in out.columns if c.startswith("feat_")])
    return out


def write_manifest(path: Path, args, n_rows: int, n_siblings: int, n_ranked: int, scanned: int) -> None:
    source = {}
    try:
        source = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("%s not found; snapshot_date will be unknown", args.manifest)
    path.write_text(json.dumps({
        "built_from": str(args.data),
        "source_dataset": args.dataset,
        "rows": n_rows,
        "sibling_key_pairs": n_siblings,
        "ranked_manufacturers": n_ranked,
        "mfr_tiers": TIERS,
        "bytes_processed": scanned,
        "limit": args.limit,
        "constants_sha256": feat.constants_sha256(),
        "alias_table_sha256": source.get("alias_table_sha256", "unknown"),
        "snapshot_date": source.get("snapshot_date", "unknown"),
        "feature_columns": list(FEATURE_COLUMNS),
    }, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="label_set_v1 -> feat_* training table")
    p.add_argument("--project", required=True)
    p.add_argument("--data", type=Path, default=ROOT / "data/label_set_v1.parquet",
                   help="label_set_v1 downloaded by s03")
    p.add_argument("--table", default="label_set_v1", help="the BigQuery table s03 downloaded")
    p.add_argument("--alias", type=Path, default=ROOT / "labels/alias_table_v1.csv")
    p.add_argument("--manifest", type=Path, default=ROOT / "labels/label_set_v1.manifest.json")
    p.add_argument("--out", type=Path, default=ROOT / "data/label_set_v1_features.parquet")
    p.add_argument("--siblings", type=Path, default=ROOT / "data/sibling_keys.csv")
    p.add_argument("--mfr-rank", type=Path, default=ROOT / "data/mfr_rank.csv")
    p.add_argument("--gudid-dataset", dest="dataset", default=bq_utils.PROD_DATASET,
                   help="source dataset for gudid and maude; vigil_md_dev for a cheap smoke test")
    p.add_argument("--limit", type=int, default=0, help="rows to build, 0 = all")
    p.add_argument("--refresh", "--refresh-siblings", dest="refresh", action="store_true",
                   help="re-query both caches instead of reusing them")
    p.add_argument("--max-gb", type=float, default=2.0)
    p.add_argument("--print-only", action="store_true", help="print the SQL; no GCP call")
    p.add_argument("--estimate-only", action="store_true", help="dry run only (free)")
    args = p.parse_args(argv)

    try:
        queries = {"sibling keys": (sibling_sql(args.project, args.dataset, args.table), args.siblings,
                                    ("mfr", "sib_key")),
                   "manufacturer rank": (mfr_rank_sql(args.project, args.dataset), args.mfr_rank,
                                         ("mfr", "rk"))}
    except ValueError as e:
        log.error("bad SQL: %s", e)
        return EXIT_BAD_SQL
    if args.print_only:
        for name, (q, _, _) in queries.items():
            print(f"-- {name}\n{q};\n")
        return EXIT_OK

    # checked before any GCP call: a missing input must not cost a query first
    if not args.estimate_only and not args.data.exists():
        log.error("%s not found. Download it first:\n"
                  "    python pipeline/s03_export_label_set.py --project %s", args.data, args.project)
        return EXIT_BAD_SQL
    for path in (args.alias,) if not args.estimate_only else ():
        if not path.exists():
            log.error("%s not found; it is committed, so check you are on the right branch", path)
            return EXIT_BAD_SQL

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)

    scanned = 0
    stale = {name: not cache.exists() or args.refresh for name, (_, cache, _) in queries.items()}
    for name, (q, _, _) in queries.items():
        if not stale[name]:
            continue
        gb = bq_utils.estimate_bytes(client, q) / 1e9          # dry run: free
        log.info("%s query would scan %.3f GB", name, gb)
        if gb > args.max_gb:
            log.error("STOP: %.3f GB is above --max-gb %.1f. Nothing was run.", gb, args.max_gb)
            return EXIT_OVER_BUDGET
    if args.estimate_only:
        return EXIT_OK

    for name, (q, cache, cols) in queries.items():
        if stale[name]:
            scanned += fetch_rows(client, q, int(args.max_gb * 1e9), cache, cols)
        else:
            log.info("%s: reusing %s. --refresh to re-query.", name, cache)
    siblings, ranks = read_siblings(args.siblings), read_mfr_rank(args.mfr_rank)
    log.info("%d labelers with sibling keys, %d ranked manufacturers", len(siblings), len(ranks))

    data = pd.read_parquet(args.data) if args.data.suffix == ".parquet" else pd.read_csv(args.data, dtype=str)
    if args.limit:
        data = data.head(args.limit)
    # one place to turn every pandas null into None, so vigil_model.rows stays pandas-free
    data = data.astype(object).where(pd.notna(data), None)
    log.info("read %d rows from %s", len(data), args.data)

    out = build(data, load_aliases(args.alias), siblings, ranks)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(args.out, index=False)
    write_manifest(args.out.with_suffix(".manifest.json"), args, len(out),
                   sum(len(v) for v in siblings.values()), len(ranks), scanned)

    fired = int(out["feat_conflicting_identifier"].sum())
    log.info("wrote %d rows x %d features to %s", len(out), len(FEATURE_COLUMNS), args.out)
    log.info("feat_conflicting_identifier is True on %d rows (0 means sibling_keys never matched)", fired)
    print(out.groupby(["meta_split", "label"]).size().to_string())
    print(out.groupby(["meta_split", "meta_mfr_tier"]).size().to_string())
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
