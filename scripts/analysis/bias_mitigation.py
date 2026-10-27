"""Bias mitigation for the MAUDE→GUDID link: reweighting the linked rows (read-only).

bias_slices.py found that the MAUDE rows that link to GUDID are not a
representative subset (e.g. Death reports and long-tail manufacturers are
under-represented). This script implements the standard correction for
linkage-induced selection bias, inverse probability weighting with weighting
cells, and measures what it achieves and costs:

  1. One read-only query counts MAUDE rows and linked rows per cell
     (event_type × manufacturer_tier × year_group × adverse_event_flag ×
     product_code). "Linked" and the tiers use bias_slices.py's own SQL
     (exact match on DISTINCT GUDID primary DIs; top 10 / 11-100 / long tail).
  2. Four weighting versions are compared:
       unweighted    w = 1
       event_type    w = N/L per event_type
       full          w = N/L per event_type × manufacturer_tier × year_group
                     (a cell with fewer than --min-linked linked rows uses the
                     next coarser level: drop year_group, then tier)
       full_trimmed  full, with weights capped at --trim × the mean weight
  3. For each version: the linked subset's share of every slice value against
     the population (all MAUDE rows), the total variation distance (TVD) per
     variable, Kish's effective sample size and the largest weight.
     adverse_event_flag and product_code are never used for weighting: they
     are the held-out check (a weighting that works should move them toward
     the population too).
  4. Fairlearn MetricFrame computes the link rate per slice (selection_rate on
     the link flag, with the cell counts as sample weights, which makes it
     exact) and is cross-checked against bias_slices.py's link_coverage.csv.

Nothing is written to BigQuery. The query is dry-run first, stops above
--max-gb, and the real job runs with maximum_bytes_billed.

Output (--out, default reports/bias/): mitigation_summary.json and
mitigation_shares.csv; the cell counts go to data/interim/bias/mitigation_cells.csv.

Usage:
    python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816 --print-only
    python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816 --estimate-only
    python scripts/analysis/bias_mitigation.py --project vigil-md-1792696816

Exit codes: 0 = done, 3 = stopped by the cost guard, anything else = unexpected error.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.analysis.bias_slices import (  # noqa: E402
    TIERS, TOP_N, _rank_cte, _slice_expr, assert_read_only)
from scripts.logger import get_logger  # noqa: E402
from scripts.warehouse import schemas  # noqa: E402

log = get_logger("bias_mitigation")
EXIT_OK, EXIT_OVER_BUDGET = 0, 3

MIN_LINKED = 30       # a cell needs this many linked rows to get its own weight
TRIM = 5.0            # full_trimmed caps weights at TRIM × mean weight
WEIGHT_VARS = ("event_type", "manufacturer_tier", "year_group")
HELD_OUT = ("adverse_event_flag", "product_code")
ALL_VARS = WEIGHT_VARS + HELD_OUT

# Weighting versions: the levels tried in order (finest first). () = overall.
VERSIONS = {
    "unweighted":   None,
    "event_type":   [("event_type",), ()],
    "full":         [WEIGHT_VARS, ("event_type", "manufacturer_tier"), ("event_type",), ()],
}
YEAR_GROUPS = (("2016_2017", 2017), ("2018_2020", 2020), ("2021_2023", 2023))  # else 2024_2026


# ── SQL ───────────────────────────────────────────────────────────────────────

def _year_group_expr(col: str = "record_date") -> str:
    parts = [f"WHEN EXTRACT(YEAR FROM t.{col}) <= {last} THEN '{name}'" for name, last in YEAR_GROUPS]
    return f"CASE WHEN t.{col} IS NULL THEN 'unknown' " + " ".join(parts) + " ELSE '2024_2026' END"


def render_cells_sql(project: str) -> str:
    """Row and linked-row counts per cell, in one scan of maude PROD."""
    maude, gudid = schemas.prod_table(project, "maude"), schemas.prod_table(project, "gudid")
    ranks = ",\n".join(_rank_cte(f"rk_maude_{c}", maude, c)
                        for c in ("manufacturer_name_norm", "product_code"))
    return f"""WITH {ranks},
gudid_di AS (SELECT DISTINCT udi_di FROM `{gudid}` WHERE udi_di IS NOT NULL)
SELECT
  {_slice_expr("maude", "event_type", "value")} AS event_type,
  {_slice_expr("maude", "manufacturer_name_norm", "tier")} AS manufacturer_tier,
  {_year_group_expr()} AS year_group,
  {_slice_expr("maude", "adverse_event_flag", "value")} AS adverse_event_flag,
  {_slice_expr("maude", "product_code", "top")} AS product_code,
  COUNT(*) AS n_rows,
  COUNTIF(g.udi_di IS NOT NULL) AS n_linked
FROM `{maude}` AS t
LEFT JOIN gudid_di AS g ON t.udi_di = g.udi_di
LEFT JOIN rk_maude_manufacturer_name_norm ON t.manufacturer_name_norm = rk_maude_manufacturer_name_norm.k
LEFT JOIN rk_maude_product_code ON t.product_code = rk_maude_product_code.k
GROUP BY 1, 2, 3, 4, 5"""


# ── Weights (pure functions on the cell counts) ───────────────────────────────
# A cell is a dict with the five slice columns plus n_rows (N) and n_linked (L).

def _key(cell: dict, level: tuple) -> tuple:
    return tuple(cell[v] for v in level)


def aggregate(cells: list[dict], level: tuple) -> dict:
    """{key: [N, L]} for one level of the weighting variables."""
    agg = defaultdict(lambda: [0, 0])
    for c in cells:
        a = agg[_key(c, level)]
        a[0] += c["n_rows"]
        a[1] += c["n_linked"]
    return dict(agg)


def cell_weights(cells: list[dict], levels: list[tuple] | None,
                 min_linked: int = MIN_LINKED) -> tuple[list[float], dict]:
    """Weight per cell (applied to its linked rows), normalised so the weighted
    linked rows add up to all rows. Also returns how many cells used each level."""
    if levels is None:
        return [1.0] * len(cells), {"unweighted": len(cells)}
    aggs = {lv: aggregate(cells, lv) for lv in levels}
    weights, used = [], defaultdict(int)
    for c in cells:
        w = 0.0
        for lv in levels:
            n, l_ = aggs[lv][_key(c, lv)]
            if l_ >= min_linked or (lv == () and l_ > 0):
                w = n / l_
                used["+".join(lv) or "overall"] += 1
                break
        weights.append(w)
    return _normalise(cells, weights), dict(used)


def _normalise(cells: list[dict], weights: list[float]) -> list[float]:
    total_n = sum(c["n_rows"] for c in cells)
    weighted_l = sum(c["n_linked"] * w for c, w in zip(cells, weights))
    return [w * total_n / weighted_l for w in weights] if weighted_l else weights


def trim_weights(cells: list[dict], weights: list[float], mult: float = TRIM) -> list[float]:
    """Cap weights at mult × the mean weight of the linked rows, then renormalise."""
    total_l = sum(c["n_linked"] for c in cells)
    mean_w = sum(c["n_linked"] * w for c, w in zip(cells, weights)) / total_l
    return _normalise(cells, [min(w, mult * mean_w) for w in weights])


# ── Evaluation ────────────────────────────────────────────────────────────────

def shares(cells: list[dict], var: str, weights: list[float] | None = None) -> dict:
    """Population share (weights=None) or weighted linked share of each value of var."""
    num, den = defaultdict(float), 0.0
    for i, c in enumerate(cells):
        m = c["n_rows"] if weights is None else c["n_linked"] * weights[i]
        num[c[var]] += m
        den += m
    return {k: v / den for k, v in num.items()} if den else {}


def tvd(p: dict, q: dict) -> float:
    """Total variation distance: half the sum of absolute share differences (0 = identical)."""
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in set(p) | set(q))


def kish_ess(cells: list[dict], weights: list[float]) -> float:
    """Kish's effective sample size of the weighted linked rows: (Σw)² / Σw²."""
    s1 = sum(c["n_linked"] * w for c, w in zip(cells, weights))
    s2 = sum(c["n_linked"] * w * w for c, w in zip(cells, weights))
    return s1 * s1 / s2 if s2 else 0.0


def evaluate(cells: list[dict], weights: list[float]) -> dict:
    total_l = sum(c["n_linked"] for c in cells)
    linked = [w for c, w in zip(cells, weights) if c["n_linked"] > 0]
    mean_w = sum(c["n_linked"] * w for c, w in zip(cells, weights)) / total_l
    ess = kish_ess(cells, weights)
    pop = {v: shares(cells, v) for v in ALL_VARS}
    lnk = {v: shares(cells, v, weights) for v in ALL_VARS}
    return {
        "effective_sample_size": round(ess),
        "ess_ratio": ess / total_l,
        "max_weight_ratio": max(linked) / mean_w,
        "tvd": {v: tvd(pop[v], lnk[v]) for v in ALL_VARS},
        "highlights": {
            "event_type=Death": [pop["event_type"].get("Death", 0.0), lnk["event_type"].get("Death", 0.0)],
            f"manufacturer_tier=top_{TIERS[0]}": [pop["manufacturer_tier"].get(f"top_{TIERS[0]}", 0.0),
                                                  lnk["manufacturer_tier"].get(f"top_{TIERS[0]}", 0.0)],
            "manufacturer_tier=long_tail": [pop["manufacturer_tier"].get("long_tail", 0.0),
                                            lnk["manufacturer_tier"].get("long_tail", 0.0)],
        },
    }, pop, lnk


def unrepresentable(cells: list[dict]) -> dict:
    """Share of all MAUDE rows whose value of each variable has no linked row at all.

    No reweighting can restore these: there is nothing to weight. They set a floor
    under the TVD that reweighting can reach for that variable.
    """
    total_n = sum(c["n_rows"] for c in cells)
    out = {}
    for var in ALL_VARS:
        agg = aggregate(cells, (var,))
        out[var] = sum(n for n, l_ in agg.values() if l_ == 0) / total_n
    return out


def run_versions(cells: list[dict], min_linked: int = MIN_LINKED, trim: float = TRIM) -> dict:
    """{version: (weights, levels_used)} for the four versions."""
    out = {name: cell_weights(cells, levels, min_linked) for name, levels in VERSIONS.items()}
    full_w, full_used = out["full"]
    out["full_trimmed"] = (trim_weights(cells, full_w, trim), full_used)
    return out


# ── Fairlearn assessment ──────────────────────────────────────────────────────

def fairlearn_link_rates(cells: list[dict], var: str) -> dict:
    """Link rate per value of var with Fairlearn MetricFrame.

    Each cell becomes two rows (linked / not linked) weighted by their counts,
    so selection_rate with sample_weight is the exact rate over all MAUDE rows.
    """
    import pandas as pd
    from fairlearn.metrics import MetricFrame, selection_rate

    rows = []
    for c in cells:
        for flag, count in ((1, c["n_linked"]), (0, c["n_rows"] - c["n_linked"])):
            if count:
                rows.append((c[var], flag, count))
    df = pd.DataFrame(rows, columns=[var, "linked", "weight"])
    mf = MetricFrame(metrics={"link_rate": selection_rate},
                     y_true=df["linked"], y_pred=df["linked"],
                     sensitive_features=df[var],
                     sample_params={"link_rate": {"sample_weight": df["weight"].to_numpy()}})
    by_group = mf.by_group["link_rate"].to_dict()
    return {"overall": float(mf.overall["link_rate"]),
            "by_group": {str(k): float(v) for k, v in by_group.items()},
            "difference": float(mf.difference()["link_rate"]),
            "ratio": float(mf.ratio()["link_rate"])}


def cross_check(fair: dict, coverage_csv: Path) -> dict:
    """Largest gap between Fairlearn's rates and bias_slices.py's link_coverage.csv."""
    if not coverage_csv.exists():
        return {"status": f"skipped: {coverage_csv} not found (run bias_slices.py first)"}
    ref = defaultdict(dict)
    with open(coverage_csv, newline="") as f:
        for r in csv.DictReader(f):
            if r["metric"] == "link_maude_to_gudid":
                ref[r["slice_name"]][r["slice_value"]] = float(r["rate"])
    gaps, compared = {}, 0
    for var, res in fair.items():
        if var not in ref:
            continue
        diffs = [abs(rate - ref[var][val]) for val, rate in res["by_group"].items() if val in ref[var]]
        compared += len(diffs)
        gaps[var] = max(diffs) if diffs else None
    return {"status": "ok", "values_compared": compared, "max_abs_gap": gaps}


# ── Output ────────────────────────────────────────────────────────────────────

def share_rows(results: dict, pop: dict) -> list[dict]:
    rows = []
    for version, (_, _, lnk) in results.items():
        for var in ALL_VARS:
            for val in sorted(set(pop[var]) | set(lnk[var])):
                p, q = pop[var].get(val, 0.0), lnk[var].get(val, 0.0)
                rows.append({"version": version, "variable": var,
                             "role": "weighting" if var in WEIGHT_VARS else "held_out",
                             "value": val, "population_share": round(p, 6),
                             "linked_share": round(q, 6), "diff_pp": round(100 * (q - p), 3)})
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def analyse(cells: list[dict], min_linked: int = MIN_LINKED, trim: float = TRIM,
            with_fairlearn: bool = True, coverage_csv: Path | None = None) -> tuple[dict, list[dict]]:
    """Everything except BigQuery: versions, evaluation, Fairlearn. Pure apart from imports."""
    total_n = sum(c["n_rows"] for c in cells)
    total_l = sum(c["n_linked"] for c in cells)
    results, summary_versions, pop = {}, {}, None
    for name, (weights, used) in run_versions(cells, min_linked, trim).items():
        ev, pop, lnk = evaluate(cells, weights)
        results[name] = (weights, ev, lnk)
        summary_versions[name] = {**ev, "levels_used": used}
    summary = {
        "maude_rows": total_n, "linked_rows": total_l, "link_rate": total_l / total_n,
        "cells": len(cells),
        "unrepresentable_share": unrepresentable(cells),
        "settings": {"weighting_variables": list(WEIGHT_VARS), "held_out_variables": list(HELD_OUT),
                     "min_linked": min_linked, "trim_multiple": trim,
                     "year_groups": [g for g, _ in YEAR_GROUPS] + ["2024_2026", "unknown"],
                     "tiers": list(TIERS), "top_n_product_codes": TOP_N},
        "versions": summary_versions,
    }
    if with_fairlearn:
        fair = {v: fairlearn_link_rates(cells, v) for v in ALL_VARS}
        summary["fairlearn_link_rates"] = fair
        if coverage_csv is not None:
            summary["cross_check_vs_bias_slices"] = cross_check(fair, coverage_csv)
    return summary, share_rows(results, pop)


def run(client, project: str, out: Path, cells_out: Path, coverage_csv: Path,
        max_gb: float, min_linked: int, trim: float, estimate_only: bool = False) -> int:
    from google.cloud import bigquery
    from scripts.validation.run_validation import estimate_bytes

    sql = render_cells_sql(project)
    assert_read_only(sql)
    est = estimate_bytes(client, sql)
    log.info("estimate cells: %.3f GB", est / 1e9)
    if est / 1e9 > max_gb:
        log.error("STOP cells: %.3f GB is above --max-gb %.1f", est / 1e9, max_gb)
        return EXIT_OVER_BUDGET
    if estimate_only:
        return EXIT_OK

    job = client.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=int(max_gb * 1e9)))
    cells = [{k: (int(v) if k in ("n_rows", "n_linked") else str(v)) for k, v in r.items()}
             for r in job.result()]
    if not cells:
        raise RuntimeError("cells query returned no rows")
    log.info("ran cells: %d cells, %.3f GB processed", len(cells),
             (getattr(job, "total_bytes_processed", 0) or 0) / 1e9)
    write_csv(cells_out, cells)

    summary, rows = analyse(cells, min_linked, trim, coverage_csv=coverage_csv)
    summary = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
               "scope": "read-only SELECT on vigil_md_prod (maude, gudid)",
               "bytes": {"estimated": est, "processed": getattr(job, "total_bytes_processed", None)},
               **summary}
    out.mkdir(parents=True, exist_ok=True)
    (out / "mitigation_summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    write_csv(out / "mitigation_shares.csv", rows)
    for name, v in summary["versions"].items():
        log.info("%-13s TVD event_type %.4f tier %.4f year %.4f | held-out flag %.4f product %.4f | "
                 "ESS %.1f%% max w %.1fx", name, v["tvd"]["event_type"], v["tvd"]["manufacturer_tier"],
                 v["tvd"]["year_group"], v["tvd"]["adverse_event_flag"], v["tvd"]["product_code"],
                 100 * v["ess_ratio"], v["max_weight_ratio"])
    log.info("wrote %s and %s", out / "mitigation_summary.json", out / "mitigation_shares.csv")
    return EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", type=Path, default=Path("reports/bias"))
    ap.add_argument("--cells-out", type=Path, default=Path("data/interim/bias/mitigation_cells.csv"))
    ap.add_argument("--coverage-csv", type=Path, default=Path("data/interim/bias/link_coverage.csv"),
                    help="bias_slices.py output used for the Fairlearn cross-check")
    ap.add_argument("--max-gb", type=float, default=2.0, help="stop if the dry run is above this")
    ap.add_argument("--min-linked", type=int, default=MIN_LINKED)
    ap.add_argument("--trim", type=float, default=TRIM)
    ap.add_argument("--estimate-only", action="store_true", help="dry run only (free)")
    ap.add_argument("--print-only", action="store_true", help="print the SQL; no GCP calls")
    args = ap.parse_args()

    if args.print_only:
        sql = render_cells_sql(args.project)
        assert_read_only(sql)
        print(sql)
        sys.exit(EXIT_OK)

    from google.cloud import bigquery
    client = bigquery.Client(project=args.project)
    sys.exit(run(client, args.project, args.out, args.cells_out, args.coverage_csv,
                 args.max_gb, args.min_linked, args.trim, args.estimate_only))


if __name__ == "__main__":
    main()
