"""Bias slice analysis of the PROD tables (read-only, PROD columns only).

Measures, per data slice:
  1. UDI availability: MAUDE device rows with a udi_di
  2. Conditional primary-DI match: of those, the share whose udi_di is a GUDID
     primary DI (the exact rule of scripts/matching/exact_linking.py, unchanged)
  3. End-to-end link coverage: MAUDE→GUDID (1 × 2), recall↔enforcement on link_key
  4. Field completeness: non-null, non-blank rate of each PROD business column

Every query is a SELECT on vigil_md_prod. Nothing reads RAW or STAGING, and
nothing is written to BigQuery. Each query is dry-run first and stops above
--max-gb; the real job also gets maximum_bytes_billed.

Output (--out, default data/interim/bias/):
  udi_availability.csv, primary_di_match.csv, unmatched_udi_shape.csv,
  link_coverage.csv, field_completeness.csv, summary.json

Usage:
    python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --print-only
    python scripts/analysis/bias_slices.py --project vigil-md-1792696816 --estimate-only
    python scripts/analysis/bias_slices.py --project vigil-md-1792696816

Exit codes: 0 = done, 3 = stopped by the cost guard, anything else = unexpected error.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.logger import get_logger                       # noqa: E402
from scripts.validation.expectations import RULES           # noqa: E402
from scripts.warehouse import schemas                       # noqa: E402

log = get_logger("bias_slices")
EXIT_OK, EXIT_OVER_BUDGET = 0, 3

TOP_N        = 25          # top-N values kept per high-cardinality slice, rest → OTHER
TIERS        = (10, 100)   # manufacturer volume tiers: top 10 / 11-100 / long tail
MIN_N        = 30          # below this a slice is a small cell (never flagged)
GAP_PP       = 10.0        # disparity: |rate - overall| at least this many points
FOUR_FIFTHS  = 0.8
LOWER_IS_BETTER = {"udi_length_out_of_rule"}   # error rates: no four-fifths, best = lowest
UDI_LEN      = RULES["maude"]["lengths"]["udi_di"][0]   # (6, 23)

# (slice_name, column, kind). kind: value | year | top (top-N + OTHER) | tier
SLICES = {
    "maude": [
        ("year",               "record_date",            "year"),
        ("event_type",         "event_type",             "value"),
        ("adverse_event_flag", "adverse_event_flag",     "value"),
        ("product_code",       "product_code",           "top"),
        ("manufacturer",       "manufacturer_name_norm", "top"),
        ("manufacturer_tier",  "manufacturer_name_norm", "tier"),
    ],
    "gudid": [
        ("publish_year",         "publish_date",           "year"),
        ("device_record_status", "device_record_status",   "value"),
        ("distribution_status",  "distribution_status",    "value"),
        ("manufacturer",         "manufacturer_name_norm", "top"),
        ("manufacturer_tier",    "manufacturer_name_norm", "tier"),
    ],
    "recall": [
        ("year",              "record_date",            "year"),
        ("recall_status",     "recall_status",          "value"),
        ("root_cause",        "root_cause",             "value"),
        ("product_code",      "product_code",           "top"),
        ("manufacturer_tier", "manufacturer_name_norm", "tier"),
    ],
    "enforcement": [
        ("year",              "record_date",            "year"),
        ("recall_class",      "recall_class",           "value"),
        ("status",            "status",                 "value"),
        ("manufacturer_tier", "manufacturer_name_norm", "tier"),
    ],
}

LIMITATIONS = [
    "Slices that exist only in RAW payloads are not analysed: device class, medical "
    "specialty / review panel, country, MAUDE report source and reporter type, patient "
    "sex and age, MAUDE date_of_event, GUDID GMDN terms and premarket submission.",
    "GUDID PROD keeps only the primary DI. Matches to secondary, package or unit-of-use "
    "DIs cannot be tested, so the match rate is 'matches the primary DI', not 'any DI'.",
    "MAUDE/GUDID → recall/enforcement has no identifier rule (only product_code and "
    "manufacturer, which would be a new matching rule), so it is not measured.",
    "MAUDE udi_di is stored unnormalized. Part of the non-match may be formatting; "
    "unmatched_udi_shape.csv sizes it as a diagnostic only, the rule is not changed.",
    "manufacturer_name_norm carries the known norm_mfr suffix bugs (docs/issue.md #13), "
    "which can split one manufacturer across slices.",
    f"High-cardinality slices keep the top {TOP_N} values; the rest are pooled as OTHER.",
    "Blank strings are reported as '(null)'. year 'unknown' = no record_date "
    "(the 1900-01-01 _part_date sentinel).",
]

CSV_FIELDS = ["metric", "table", "field", "slice_name", "slice_value", "n", "numerator",
              "rate", "ci_low", "ci_high", "overall_rate", "gap_pp", "ratio_to_overall",
              "small_cell", "below_four_fifths", "disparity_flag"]

READ_ONLY_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE|ALTER|EXPORT)\b", re.I)


# ── SQL ───────────────────────────────────────────────────────────────────────

def _rank_cte(name: str, table: str, col: str) -> str:
    return (f"{name} AS (SELECT {col} AS k, ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, {col}) AS rk "
            f"FROM `{table}` WHERE NULLIF(TRIM({col}), '') IS NOT NULL GROUP BY {col})")


def _slice_expr(source: str, col: str, kind: str) -> str:
    rk = f"rk_{source}_{col}.rk"
    if kind == "year":
        return f"IFNULL(CAST(EXTRACT(YEAR FROM t.{col}) AS STRING), 'unknown')"
    if kind == "value":
        return f"COALESCE(NULLIF(TRIM(CAST(t.{col} AS STRING)), ''), '(null)')"
    if kind == "top":
        return (f"CASE WHEN {rk} IS NULL THEN '(null)' WHEN {rk} <= {TOP_N} THEN t.{col} "
                f"ELSE 'OTHER' END")
    if kind == "tier":
        return (f"CASE WHEN {rk} IS NULL THEN '(null)' WHEN {rk} <= {TIERS[0]} THEN 'top_{TIERS[0]}' "
                f"WHEN {rk} <= {TIERS[1]} THEN '{TIERS[0] + 1}_{TIERS[1]}' ELSE 'long_tail' END")
    raise ValueError(f"unknown slice kind: {kind}")


def slice_parts(project: str, source: str) -> tuple[list[str], str, str]:
    """(rank CTEs, LEFT JOINs onto alias t, ARRAY of slice STRUCTs) for one table."""
    table = schemas.prod_table(project, source)
    ranked = sorted({col for _, col, kind in SLICES[source] if kind in ("top", "tier")})
    ctes = [_rank_cte(f"rk_{source}_{c}", table, c) for c in ranked]
    joins = "\n  ".join(f"LEFT JOIN rk_{source}_{c} ON t.{c} = rk_{source}_{c}.k" for c in ranked)
    structs = ["STRUCT('overall' AS slice_name, 'all' AS slice_value)"] + [
        f"STRUCT('{name}' AS slice_name, {_slice_expr(source, col, kind)} AS slice_value)"
        for name, col, kind in SLICES[source]]
    return ctes, joins, "[\n    " + ",\n    ".join(structs) + "\n  ]"


def render_maude_link_sql(project: str) -> str:
    """UDI availability, primary-DI match and MAUDE→GUDID coverage in one scan.

    The match is exact equality with the distinct set of GUDID primary DIs
    (no TRIM/UPPER, as in exact_linking.py). DISTINCT avoids fan-out: udi_di
    is not unique in gudid.
    """
    ctes, joins, slices = slice_parts(project, "maude")
    cte_sql = ",\n".join(ctes)
    return f"""WITH {cte_sql},
gudid_di AS (SELECT DISTINCT udi_di FROM `{schemas.prod_table(project, "gudid")}` WHERE udi_di IS NOT NULL),
base AS (
  SELECT
    t.udi_di IS NOT NULL AS has_udi,
    g.udi_di IS NOT NULL AS matched,
    t.udi_di IS NOT NULL AND LENGTH(t.udi_di) NOT BETWEEN {UDI_LEN[0]} AND {UDI_LEN[1]} AS bad_length,
    {slices} AS slices
  FROM `{schemas.prod_table(project, "maude")}` AS t
  LEFT JOIN gudid_di AS g ON t.udi_di = g.udi_di
  {joins}
)
SELECT s.slice_name, s.slice_value, COUNT(*) AS n_rows, COUNTIF(has_udi) AS n_udi,
       COUNTIF(matched) AS n_matched, COUNTIF(bad_length) AS n_bad_length
FROM base, UNNEST(base.slices) AS s
GROUP BY 1, 2"""


# Plain template, not an f-string: the regex quantifiers use braces.
_SHAPE_SQL = r"""WITH gudid_di AS (SELECT DISTINCT udi_di FROM `{gudid}` WHERE udi_di IS NOT NULL),
gudid_upper AS (SELECT DISTINCT UPPER(udi_di) AS k FROM gudid_di),
unmatched AS (
  SELECT t.udi_di FROM `{maude}` AS t
  LEFT JOIN gudid_di AS g ON t.udi_di = g.udi_di
  WHERE t.udi_di IS NOT NULL AND g.udi_di IS NULL
),
shaped AS (
  SELECT
    CASE
      WHEN udi_di != TRIM(udi_di)                 THEN 'whitespace'
      WHEN STARTS_WITH(udi_di, '(01)')            THEN 'gs1_prefix_(01)'
      WHEN REGEXP_CONTAINS(udi_di, r'^01\d{14}')  THEN 'gs1_prefix_01_no_parens'
      WHEN LENGTH(udi_di) > {max_len}             THEN 'longer_than_{max_len}'
      WHEN LENGTH(udi_di) < {min_len}             THEN 'shorter_than_{min_len}'
      WHEN udi_di != UPPER(udi_di)                THEN 'lower_case'
      WHEN REGEXP_CONTAINS(udi_di, r'[^0-9A-Z+$/.\-]') THEN 'unexpected_chars'
      ELSE 'well_formed_not_in_gudid'
    END AS shape,
    COALESCE(REGEXP_EXTRACT(udi_di, r'\(01\)\s*(\d{14})'),
             REGEXP_EXTRACT(udi_di, r'^01(\d{14})'),
             UPPER(REGEXP_REPLACE(udi_di, r'\s', ''))) AS normalized
  FROM unmatched
)
SELECT shape, COUNT(*) AS n_unmatched, COUNTIF(u.k IS NOT NULL) AS n_would_match_normalized
FROM shaped LEFT JOIN gudid_upper AS u ON shaped.normalized = u.k
GROUP BY 1"""


def render_unmatched_shape_sql(project: str) -> str:
    """Diagnostic only: why unmatched MAUDE UDIs did not match. The rule is unchanged;
    n_would_match_normalized sizes what a normalization step could recover."""
    return (_SHAPE_SQL.replace("{gudid}", schemas.prod_table(project, "gudid"))
                      .replace("{maude}", schemas.prod_table(project, "maude"))
                      .replace("{max_len}", str(UDI_LEN[1]))
                      .replace("{min_len}", str(UDI_LEN[0])))


def render_link_sql(project: str) -> str:
    """Recall↔enforcement coverage on link_key (recall_number = product_res_number)."""
    r_ctes, r_joins, r_slices = slice_parts(project, "recall")
    e_ctes, e_joins, e_slices = slice_parts(project, "enforcement")
    recall, enf = schemas.prod_table(project, "recall"), schemas.prod_table(project, "enforcement")
    cte_sql = ",\n".join(r_ctes + e_ctes)
    return f"""WITH {cte_sql},
enf_keys AS (SELECT DISTINCT link_key FROM `{enf}` WHERE link_key IS NOT NULL),
recall_keys AS (SELECT DISTINCT link_key FROM `{recall}` WHERE link_key IS NOT NULL),
recall_side AS (
  SELECT 'recall_to_enforcement' AS link_name, k.link_key IS NOT NULL AS linked,
    {r_slices} AS slices
  FROM `{recall}` AS t
  LEFT JOIN enf_keys AS k ON t.link_key = k.link_key
  {r_joins}
),
enf_side AS (
  SELECT 'enforcement_to_recall' AS link_name, k.link_key IS NOT NULL AS linked,
    {e_slices} AS slices
  FROM `{enf}` AS t
  LEFT JOIN recall_keys AS k ON t.link_key = k.link_key
  {e_joins}
)
SELECT b.link_name, s.slice_name, s.slice_value, COUNT(*) AS n_rows, COUNTIF(b.linked) AS n_linked
FROM (SELECT * FROM recall_side UNION ALL SELECT * FROM enf_side) AS b, UNNEST(b.slices) AS s
GROUP BY 1, 2, 3"""


def _present(col: str, typ: str, mode: str) -> str:
    if mode == "REPEATED":
        return f"ARRAY_LENGTH(t.{col}) > 0"
    if typ == "STRING":
        return f"NULLIF(TRIM(t.{col}), '') IS NOT NULL"
    return f"t.{col} IS NOT NULL"


def render_completeness_sql(project: str, source: str) -> str:
    """Non-null, non-blank count of every PROD business column, per slice."""
    ctes, joins, slices = slice_parts(project, source)
    cols = schemas.FLAT_COLUMNS[source]
    flags = ",\n    ".join(f"{_present(c, t, m)} AS p_{c}" for c, t, m in cols)
    counts = ", ".join(f"COUNTIF(p_{c}) AS c_{c}" for c, _, _ in cols)
    cte_sql = ",\n".join(ctes)
    return f"""WITH {cte_sql},
base AS (
  SELECT
    {flags},
    {slices} AS slices
  FROM `{schemas.prod_table(project, source)}` AS t
  {joins}
)
SELECT s.slice_name, s.slice_value, COUNT(*) AS n_rows, {counts}
FROM base, UNNEST(base.slices) AS s
GROUP BY 1, 2"""


def render_snapshot_sql(project: str) -> str:
    """Row count and newest last_updated_at per PROD table (the data snapshot)."""
    return "\nUNION ALL\n".join(
        f"SELECT '{s}' AS source, COUNT(*) AS n_rows, MAX(last_updated_at) AS last_updated_at "
        f"FROM `{schemas.prod_table(project, s)}`" for s in schemas.SOURCES)


def assert_read_only(sql: str) -> None:
    """Raise unless the SQL is a single SELECT that reads only vigil_md_prod."""
    head = sql.lstrip().split(None, 1)[0].upper()
    if head not in ("SELECT", "WITH"):
        raise ValueError(f"not a SELECT: {sql[:60]!r}")
    if READ_ONLY_FORBIDDEN.search(sql):
        raise ValueError(f"write statement in analysis SQL: {READ_ONLY_FORBIDDEN.search(sql).group(0)}")
    datasets = set(re.findall(r"`[^`.]+\.([^`.]+)\.[^`]+`", sql))
    if datasets != {schemas.DATASETS["prod"]}:
        raise ValueError(f"analysis SQL must read only {schemas.DATASETS['prod']}, found {sorted(datasets)}")


def queries(project: str) -> list[tuple[str, str]]:
    """(name, sql) for every query the analysis runs, each checked read-only."""
    qs = [("snapshot", render_snapshot_sql(project)),
          ("maude_link", render_maude_link_sql(project)),
          ("unmatched_shape", render_unmatched_shape_sql(project)),
          ("recall_enforcement_link", render_link_sql(project))]
    qs += [(f"completeness_{s}", render_completeness_sql(project, s)) for s in schemas.SOURCES]
    for _, sql in qs:
        assert_read_only(sql)
    return qs


# ── Statistics ────────────────────────────────────────────────────────────────

def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for k successes out of n."""
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def score(rows: list[dict]) -> list[dict]:
    """Add rate, CI, gap to the overall row of the same (metric, table, field), and flags."""
    overall = {(r["metric"], r["table"], r["field"]): r for r in rows if r["slice_name"] == "overall"}
    out = []
    for r in rows:
        key = (r["metric"], r["table"], r["field"])
        if key not in overall:
            raise ValueError(f"no overall row for {key}")
        o = overall[key]
        o_rate = o["numerator"] / o["n"] if o["n"] else None
        n, k = r["n"], r["numerator"]
        rate = k / n if n else None
        lo, hi = wilson(k, n) if n else (None, None)
        is_slice = r["slice_name"] != "overall"
        gap = (rate - o_rate) * 100 if rate is not None and o_rate is not None else None
        ratio = rate / o_rate if rate is not None and o_rate else None
        small = n < MIN_N
        flag = (is_slice and not small and gap is not None and abs(gap) >= GAP_PP
                and not (lo <= o_rate <= hi))
        out.append({**r, "rate": rate, "ci_low": lo, "ci_high": hi, "overall_rate": o_rate,
                    "gap_pp": gap, "ratio_to_overall": ratio, "small_cell": small,
                    "below_four_fifths": bool(is_slice and not small and ratio is not None
                                              and ratio < FOUR_FIFTHS
                                              and r["metric"] not in LOWER_IS_BETTER),
                    "disparity_flag": bool(flag)})
    return out


def _row(metric, table, field, raw, n, k) -> dict:
    return {"metric": metric, "table": table, "field": field, "slice_name": raw["slice_name"],
            "slice_value": raw["slice_value"], "n": int(n), "numerator": int(k)}


def maude_rows(raw: list[dict]) -> dict[str, list[dict]]:
    """Split the maude_link result into the three metric tables."""
    avail, match, link = [], [], []
    for r in raw:
        avail.append(_row("udi_availability", "maude", "udi_di", r, r["n_rows"], r["n_udi"]))
        avail.append(_row("udi_length_out_of_rule", "maude", "udi_di", r, r["n_udi"], r["n_bad_length"]))
        match.append(_row("primary_di_match", "maude", "udi_di", r, r["n_udi"], r["n_matched"]))
        link.append(_row("link_maude_to_gudid", "maude", "udi_di", r, r["n_rows"], r["n_matched"]))
    return {"udi_availability": avail, "primary_di_match": match, "link": link}


def link_rows(raw: list[dict]) -> list[dict]:
    return [_row(f"link_{r['link_name']}", r["link_name"].split("_to_")[0], "link_key",
                 r, r["n_rows"], r["n_linked"]) for r in raw]


def completeness_rows(source: str, raw: list[dict]) -> list[dict]:
    """One row per (slice, field). A field is skipped for slices built from that field."""
    slice_col = {name: col for name, col, _ in SLICES[source]}
    out = []
    for r in raw:
        for col, _, _ in schemas.FLAT_COLUMNS[source]:
            if slice_col.get(r["slice_name"]) == col:
                continue
            out.append(_row("completeness", source, col, r, r["n_rows"], r[f"c_{col}"]))
    return out


# ── Output ────────────────────────────────────────────────────────────────────

def _fmt(v):
    if isinstance(v, float):
        return "" if math.isnan(v) else f"{v:.6f}"
    return "" if v is None else v


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r[k]) for k in CSV_FIELDS})


def _brief(r: dict) -> dict:
    return {k: r[k] for k in ("metric", "table", "field", "slice_name", "slice_value", "n",
                              "rate", "overall_rate", "gap_pp", "ratio_to_overall")}


def build_summary(scored: dict[str, list[dict]], shapes: list[dict], snapshot: list[dict],
                  bytes_by_query: dict[str, dict]) -> dict:
    all_rows = [r for rows in scored.values() for r in rows]
    overall = {f"{r['metric']}:{r['table']}:{r['field']}": r["rate"]
               for r in all_rows if r["slice_name"] == "overall"}
    # Grouped per metric so the many completeness fields do not crowd out the rest.
    disparities = {}
    for r in sorted((r for r in all_rows if r["disparity_flag"]), key=lambda r: -abs(r["gap_pp"])):
        disparities.setdefault(r["metric"], []).append(r)
    extremes = {}
    for r in all_rows:
        if r["metric"] == "completeness" or r["slice_name"] == "overall" or r["small_cell"] \
                or r["rate"] is None:
            continue
        e = extremes.setdefault(r["metric"], {"best": r, "worst": r})
        sign = -1 if r["metric"] in LOWER_IS_BETTER else 1
        if sign * r["rate"] > sign * e["best"]["rate"]:
            e["best"] = r
        if sign * r["rate"] < sign * e["worst"]["rate"]:
            e["worst"] = r
    worst_fields = sorted((r for r in scored["field_completeness"] if r["slice_name"] == "overall"),
                          key=lambda r: r["rate"])[:15]
    n_unmatched = sum(s["n_unmatched"] for s in shapes)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "scope": "read-only SELECTs on vigil_md_prod; PROD columns only",
        "thresholds": {"min_n": MIN_N, "gap_pp": GAP_PP, "four_fifths": FOUR_FIFTHS,
                       "top_n": TOP_N, "tiers": list(TIERS), "udi_length_rule": list(UDI_LEN)},
        "snapshot": [{"source": s["source"], "n_rows": s["n_rows"],
                      "last_updated_at": str(s["last_updated_at"])} for s in snapshot],
        "overall_rates": overall,
        "disparity_count": sum(len(v) for v in disparities.values()),
        "disparities": {m: {"count": len(v), "top": [_brief(r) for r in v[:25]]}
                        for m, v in disparities.items()},
        "best_worst_slice": {m: {k: _brief(v) for k, v in e.items()} for m, e in extremes.items()},
        "least_complete_fields": [_brief(r) for r in worst_fields],
        "unmatched_udi_shape": {"n_unmatched": n_unmatched, "shapes": shapes},
        "bytes": bytes_by_query,
        "limitations": LIMITATIONS,
    }


# ── Run ───────────────────────────────────────────────────────────────────────

def run(client, project: str, out: Path, max_gb: float, estimate_only: bool = False) -> int:
    from google.cloud import bigquery
    from scripts.validation.run_validation import estimate_bytes

    qs = queries(project)
    estimates = {}
    for name, sql in qs:
        estimates[name] = estimate_bytes(client, sql)
        log.info("estimate %s: %.3f GB", name, estimates[name] / 1e9)
    over = {n: b for n, b in estimates.items() if b / 1e9 > max_gb}
    if over:
        for n, b in over.items():
            log.error("STOP %s: %.3f GB is above --max-gb %.1f", n, b / 1e9, max_gb)
        return EXIT_OVER_BUDGET
    if estimate_only:
        return EXIT_OK

    results, bytes_by_query = {}, {}
    for name, sql in qs:
        job = client.query(sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=int(max_gb * 1e9)))
        rows = [dict(r.items()) for r in job.result()]
        if not rows:
            raise RuntimeError(f"{name}: query returned no rows")
        results[name] = rows
        bytes_by_query[name] = {"estimated": estimates[name],
                                "processed": getattr(job, "total_bytes_processed", None)}
        log.info("ran %s: %d rows", name, len(rows))

    m = maude_rows(results["maude_link"])
    scored = {
        "udi_availability": score(m["udi_availability"]),
        "primary_di_match": score(m["primary_di_match"]),
        "link_coverage":    score(m["link"] + link_rows(results["recall_enforcement_link"])),
        "field_completeness": score([r for s in schemas.SOURCES
                                     for r in completeness_rows(s, results[f"completeness_{s}"])]),
    }
    shapes = sorted(results["unmatched_shape"], key=lambda s: -s["n_unmatched"])

    out.mkdir(parents=True, exist_ok=True)
    for name, rows in scored.items():
        write_csv(out / f"{name}.csv", rows)
    with (out / "unmatched_udi_shape.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["shape", "n_unmatched", "n_would_match_normalized"])
        w.writeheader()
        w.writerows(shapes)
    summary = build_summary(scored, shapes, results["snapshot"], bytes_by_query)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("wrote %s (%d disparity flags)", out, summary["disparity_count"])
    return EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", type=Path, default=Path("data/interim/bias"))
    ap.add_argument("--max-gb", type=float, default=2.0,
                    help="stop if the dry run estimates more than this per query")
    ap.add_argument("--estimate-only", action="store_true",
                    help="print the cost estimates and stop")
    ap.add_argument("--print-only", action="store_true",
                    help="print the SQL; no GCP call")
    args = ap.parse_args()

    if args.print_only:
        for name, sql in queries(args.project):
            print(f"-- {name}\n{sql};\n")
        return

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    sys.exit(run(client, args.project, args.out, args.max_gb, args.estimate_only))


if __name__ == "__main__":
    main()
