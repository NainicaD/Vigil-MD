"""Matching data monitor: does new MAUDE input still look like what the matcher learned from?

Pure functions (no I/O); scripts/m01_data_drift.py runs the query and writes the files.

Windows (MAUDE PROD rows; _part_date is the report month, the table's partition):
  baseline  report months in the 12 months before the label snapshot, rows loaded by the snapshot
            (labels/label_set_v2.manifest.json "snapshot_date"): what the model learned from
  current   rows first loaded AFTER the snapshot, in the last `current_months` report months
            (partition pruning keeps the scan small). New reports arrive in small, irregular
            batches and land in the newest 2-3 report months (230 rows between 2026-10-26 and
            2026-11-19, all in 2026-07..09 except one); a late report for an older month is
            left out of the current window.
  revisit   the baseline window again, as the rows are NOW: catches in-place changes to rows the
            model learned from (openFDA re-exports rewrite old reports; the 2026-11-17 export
            blanked the UDI-DI of about 1.4M PROD rows, which a new-rows-only check cannot see)

Signals: identifier coverage (UDI rate), exact UDI links to GUDID (link rate: a decay signal,
no hand labels needed), missing model / catalog / manufacturer / product code, and the
product-code and manufacturer mix (PSI over the baseline's top_k keys plus "other").
"""
from __future__ import annotations

import datetime as dt
import math

EPS = 1e-4
RATES = ("udi_rate", "udi_link_rate", "no_model_rate", "no_catalog_rate",
         "no_manufacturer_rate", "no_product_code_rate")
MIXES = ("product_codes", "manufacturers")


def month_start(d: dt.date, months_back: int = 0) -> dt.date:
    y, m = d.year, d.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return dt.date(y, m, 1)


def baseline_window(snapshot: dt.date) -> str:
    """SQL condition: the 12 report months before the snapshot's month, loaded by the snapshot."""
    start, end = month_start(snapshot, 12), month_start(snapshot)
    return (f"_part_date >= DATE '{start}' AND _part_date < DATE '{end}' "
            f"AND first_loaded_at <= TIMESTAMP '{snapshot} 23:59:59+00'")


def current_window(snapshot: dt.date, today: dt.date, current_months: int = 8) -> str:
    """SQL condition: rows first loaded after the snapshot, in the last current_months months."""
    return (f"_part_date >= DATE '{month_start(today, current_months)}' "
            f"AND first_loaded_at > TIMESTAMP '{snapshot} 23:59:59+00'")


def profile(row: dict) -> dict:
    """The query's one row (totals, product_codes, manufacturers) -> rates and counts."""
    t = row["totals"]
    n, n_udi = int(t["n"]), int(t["n_udi"])

    def rate(k, d):
        return round(int(t[k]) / d, 6) if d else None
    return {
        "n": n, "n_udi": n_udi,
        "rates": {"udi_rate": rate("n_udi", n),
                  "udi_link_rate": rate("n_udi_in_gudid", n_udi),
                  "no_model_rate": rate("n_no_model", n),
                  "no_catalog_rate": rate("n_no_catalog", n),
                  "no_manufacturer_rate": rate("n_no_manufacturer", n),
                  "no_product_code_rate": rate("n_no_product_code", n)},
        "counts": {mix: {r["k"]: int(r["n"]) for r in row.get(mix) or []} for mix in MIXES},
    }


def mix_shares(counts: dict[str, int], n: int, keys: list[str]) -> list[float]:
    """Shares of the keys, then "other" (everything else in n)."""
    if not n:
        return []
    shares = [counts.get(k, 0) / n for k in keys]
    return shares + [max(0.0, 1.0 - sum(shares))]


def psi(expected: list[float], actual: list[float]) -> float | None:
    if not expected or not actual or len(expected) != len(actual):
        return None
    return round(sum((max(a, EPS) - max(e, EPS)) * math.log(max(a, EPS) / max(e, EPS))
                     for e, a in zip(expected, actual)), 4)


def _level(value, warn, drift):
    if value is None:
        return "n/a"
    return "drift" if value >= drift else "warn" if value >= warn else "ok"


def compare(baseline: dict, current: dict, limits: dict) -> dict:
    """Checks and result: drift | warn | ok | insufficient_data (fewer than min_rows rows)."""
    checks = []
    for name in RATES:
        b, c = baseline["rates"][name], current["rates"][name]
        value = None if b is None or c is None else round(abs(c - b), 4)
        drift = limits["udi_link_rate_drift"] if name == "udi_link_rate" else limits["rate_drift"]
        checks.append({"name": name, "kind": "rate", "baseline": b, "current": c, "value": value,
                       "level": _level(value, min(limits["rate_warn"], drift), drift)})
    for mix in MIXES:
        keys = sorted(baseline["counts"][mix], key=lambda k: -baseline["counts"][mix][k])
        keys = keys[:limits["top_k"]]
        value = psi(mix_shares(baseline["counts"][mix], baseline["n"], keys),
                    mix_shares(current["counts"][mix], current["n"], keys))
        checks.append({"name": mix, "kind": "psi", "value": value,
                       "level": _level(value, limits["psi_warn"], limits["psi_drift"])})
    if current["n"] < limits["min_rows"]:
        result = "insufficient_data"
    elif any(c["level"] == "drift" for c in checks):
        result = "drift"
    elif any(c["level"] == "warn" for c in checks):
        result = "warn"
    else:
        result = "ok"
    return {"result": result, "n": current["n"], "min_rows": limits["min_rows"], "checks": checks}


def overall(current: dict, revisit: dict | None) -> str:
    """One result from both checks. Drift in either is drift. The revisit window is large and
    always decides; the current window decides only with min_rows rows."""
    results = [current["result"]] + ([revisit["result"]] if revisit else [])
    for level in ("drift", "warn"):
        if level in results:
            return level
    return current["result"]


def simulate(baseline: dict, kind: str, n: int = 20_000) -> dict:
    """A synthetic current profile for demos and tests (no query): the baseline's shape at size
    n, then one shift. kinds: none, udi_drop (UDI rate halves, link rate -0.2), new_devices
    (40% of rows move to product codes the model never saw)."""
    scale = n / baseline["n"] if baseline["n"] else 0
    cur = {"n": n, "n_udi": int(baseline["n_udi"] * scale), "rates": dict(baseline["rates"]),
           "counts": {mix: {k: int(v * scale) for k, v in baseline["counts"][mix].items()}
                      for mix in MIXES}}
    if kind == "udi_drop":
        cur["rates"]["udi_rate"] = round(baseline["rates"]["udi_rate"] / 2, 6)
        cur["rates"]["udi_link_rate"] = round(max(0.0, baseline["rates"]["udi_link_rate"] - 0.2), 6)
    elif kind == "new_devices":
        pc = cur["counts"]["product_codes"]
        cur["counts"]["product_codes"] = {k: int(v * 0.6) for k, v in pc.items()}
        cur["counts"]["product_codes"]["ZZ_NEW"] = int(n * 0.4)
    elif kind != "none":
        raise ValueError(f"unknown simulation {kind!r}: none, udi_drop, new_devices")
    return cur
