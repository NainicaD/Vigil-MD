"""Compare the model's links with the known answer on rows that have an exact UDI link.

Input: one udi_check run. Candidates from s13 --mode udi_check (cand_group_key, true_group_key,
meta_n_pool_rows) and the MATCH links that s12 wrote for them. The model never sees the UDI (a
banned feature), so the row's own UDI tells whether a link is right. A link is right when its
candidate's GUDID model group is the group of the row's UDI: the same-device definition is at the
model level (docs/SAME_DEVICE_DEFINITION.md section 1).

Output (--out, JSON), sent to ELK as "matching_udi_agreement" by the vigil_md_matching_check DAG:
  n_rows             sampled MAUDE rows (with or without candidates)
  candidate_recall   rows whose true group is among their candidates / n_rows
  precision          MATCH links to the true group / MATCH links
  recall             rows with a MATCH link to the true group / n_rows
  model_recall       recall / candidate_recall (what the model finds of what it was shown)

  slices             the same precision and model recall per slice of the rows (slices()): whether
                     the report names a model number, one or several matched model groups, top-10
                     manufacturers or not, each large manufacturer and product code, the link's
                     match kind, the number of candidates, and the event type (when s13 wrote it)
  slice_flags        the slices with enough links whose precision is clearly under a level: the
                     whole 95% Wilson interval is under --warn (0.95) or --alert (0.90)

Limit: rows with a UDI are easier than the pool (rows without one). Read these numbers as a
trend between runs and model versions, not as the accuracy on the pool. Slice recall is over the
rows that got candidates (a sampled row without candidates is not in the files).

File in, file out: no BigQuery.

Usage (from anywhere):
    python scripts/s14_udi_agreement.py --candidates <dir>/candidates.parquet \
        --links <dir>/links.parquet --bundle models/matching-same-device-v1.1.0 --out <dir>/check_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import math
import re

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.s12_score_pairs import best_link_per_device  # noqa: E402


WARN, ALERT = 0.95, 0.90          # the levels of the overall check (Kibana warn / alert rules)
MIN_LINKS = 200                   # a slice with fewer links is reported, never flagged
TOP_MANUFACTURERS = 10
# s13's rule for a missing model or catalog number (sql/scoring/01_candidates.sql, keyed)
_PLACEHOLDER = re.compile(r"^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|"
                          r"UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$")


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """The 95% Wilson interval of k successes in n (None for n = 0). Pure."""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)


def _has_number(value) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return False
    key = re.sub(r"[\s\-_./]", "", str(value).strip()).upper()
    return len(key) >= 2 and not _PLACEHOLDER.match(key)


def _level(precision_ci, n_links: int, warn: float, alert: float, min_links: int) -> str:
    if n_links < min_links or precision_ci is None:
        return "small"
    if precision_ci[1] < alert:
        return "alert"
    if precision_ci[1] < warn:
        return "warn"
    return "ok"


def slices(cands: pd.DataFrame, links: pd.DataFrame, warn: float = WARN, alert: float = ALERT,
           min_links: int = MIN_LINKS) -> list[dict]:
    """Precision and model recall per slice of the sampled rows that got candidates, measured as
    agreement() does (one link per reported device, right = the true model group). Each slice:
    slice, value, rows, links, links_right, precision, precision_ci (95% Wilson), model_recall
    (None for match_kind: only linked rows have one) and level: small (fewer than min_links
    links), alert / warn (the whole interval is under that level) or ok. Per-manufacturer and
    per-product-code slices are listed only from min_links links. Pure."""
    c = cands.drop_duplicates(["maude_id", "gudid_id"])
    first = c.groupby("maude_id").first()
    rows = pd.DataFrame(index=first.index)
    # a column s13 did not write (an older file, a test) gives no slice of it
    if "key_m_mfr" in first:
        rows["manufacturer"] = first["key_m_mfr"].fillna("(none)")
    if "key_m_pc" in first:
        rows["product_code"] = first["key_m_pc"].fillna("(none)")
    if "key_m_model_raw" in first:
        rows["model_number"] = [("has model number" if _has_number(m) else "no model number")
                                for m in first["key_m_model_raw"]]
    n_cands = c.groupby("maude_id").size()
    rows["candidates"] = pd.cut(n_cands, [0, 1, 3, 10, math.inf],
                                labels=["1", "2-3", "4-10", "more than 10"]).astype(str)
    if "meta_event_type" in c.columns:
        rows["event_type"] = first["meta_event_type"].fillna("(none)")
    rows["found"] = rows.index.isin(c.loc[c["cand_group_key"] == c["true_group_key"], "maude_id"])

    keyed_all = links.merge(c, left_on=["left_id", "right_id"], right_on=["maude_id", "gudid_id"],
                            how="inner")
    groups = keyed_all.groupby("left_id")["cand_group_key"].nunique()
    rows["matches"] = ["2 or more model groups" if groups.get(i, 0) >= 2 else "0 or 1 model group"
                       for i in rows.index]
    best = best_link_per_device(links).merge(
        c, left_on=["left_id", "right_id"], right_on=["maude_id", "gudid_id"], how="inner")
    best = best.drop_duplicates("left_id").set_index("left_id")
    rows["linked"] = rows.index.isin(best.index)
    rows["right"] = False
    rows.loc[rows["linked"], "right"] = (best.loc[rows.index[rows["linked"]], "cand_group_key"]
                                         == best.loc[rows.index[rows["linked"]], "true_group_key"]).values
    rows["match_kind"] = best["match_kind"].reindex(rows.index) if "match_kind" in best else None
    if "manufacturer" in rows:
        top = set(rows["manufacturer"].value_counts().head(TOP_MANUFACTURERS).index)
        rows["manufacturer_tier"] = ["top 10 manufacturers" if m in top else "other manufacturers"
                                     for m in rows["manufacturer"]]

    out = []
    for field in ("model_number", "matches", "manufacturer_tier", "candidates", "event_type",
                  "match_kind", "manufacturer", "product_code"):
        if field not in rows or rows[field].isna().all():
            continue
        for value, g in rows.groupby(field, sort=True):
            n_links, n_right = int(g["linked"].sum()), int(g["right"].sum())
            if field in ("manufacturer", "product_code") and n_links < min_links:
                continue
            ci = wilson(n_right, n_links)
            n_found = int(g["found"].sum())
            out.append({"slice": field, "value": str(value), "rows": len(g), "links": n_links,
                        "links_right": n_right,
                        "precision": round(n_right / n_links, 6) if n_links else None,
                        "precision_ci": list(ci) if ci else None,
                        "model_recall": (None if field == "match_kind" else
                                         round(n_right / n_found, 6) if n_found else None),
                        "level": _level(ci, n_links, warn, alert, min_links)})
    return out


def slice_flags(slice_rows: list[dict]) -> list[str]:
    """The flagged slices as one line each, alerts first, then by precision. Pure."""
    bad = [s for s in slice_rows if s["level"] in ("warn", "alert")]
    bad.sort(key=lambda s: (s["level"] != "alert", s["precision"]))
    return [f"{s['level']}: {s['slice']} = {s['value']}: precision {s['precision']:.3f} "
            f"(95% CI {s['precision_ci'][0]:.3f}-{s['precision_ci'][1]:.3f}, {s['links']:,} links)"
            for s in bad]


def agreement(cands: pd.DataFrame, links: pd.DataFrame) -> dict:
    """The check's numbers from the candidates and the MATCH links of one udi_check run, after the
    loader's rule (one inferred link per reported device), so it measures the links as loaded. Pure."""
    if cands["true_group_key"].isna().all():
        raise SystemExit("no true_group_key: these candidates are not from s13 --mode udi_check")
    n_links_all = len(links)
    links = best_link_per_device(links)
    n_rows = int(cands["meta_n_pool_rows"].iloc[0])
    right = cands[cands["cand_group_key"] == cands["true_group_key"]]
    # a DI listed twice in GUDID can represent two groups: count each link once
    keyed = links.merge(cands.drop_duplicates(["maude_id", "gudid_id"]),
                        left_on=["left_id", "right_id"], right_on=["maude_id", "gudid_id"], how="left")
    correct = keyed[keyed["cand_group_key"] == keyed["true_group_key"]]
    rows_found = right["maude_id"].nunique()
    rows_linked_right = correct["left_id"].nunique()

    def share(a: int, b: int) -> float | None:
        return round(a / b, 6) if b else None

    return {"n_rows": n_rows, "n_rows_with_candidates": int(cands["maude_id"].nunique()),
            "n_pairs": len(cands), "n_links_all": n_links_all, "n_links": len(links),
            "n_links_right": len(correct),
            "candidate_recall": share(rows_found, n_rows),
            "precision": share(len(correct), len(links)),
            "recall": share(rows_linked_right, n_rows),
            "model_recall": share(rows_linked_right, rows_found)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidates", type=Path, required=True, help="s13 --mode udi_check parquet")
    ap.add_argument("--links", type=Path, required=True, help="s12 output for those candidates")
    ap.add_argument("--bundle", type=Path, required=True, help="the bundle s12 used (meta.json)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ie-gate", help="the gate s12 applied (s12 --ie-gate); default: the bundle's")
    ap.add_argument("--warn", type=float, default=WARN, help="slice warning level (precision)")
    ap.add_argument("--alert", type=float, default=ALERT, help="slice alert level (precision)")
    ap.add_argument("--min-links", type=int, default=MIN_LINKS, help="links a slice needs to be flagged")
    a = ap.parse_args(argv)

    cands, links = pd.read_parquet(a.candidates), pd.read_parquet(a.links)
    report = agreement(cands, links)
    report["slices"] = slices(cands, links, a.warn, a.alert, a.min_links)
    report["slice_flags"] = slice_flags(report["slices"])
    report["slice_levels"] = {"warn": a.warn, "alert": a.alert, "min_links": a.min_links}
    meta = json.loads((a.bundle / "meta.json").read_text())
    report.update(bundle_version=meta.get("version", "unknown"), cutoff=float(meta["cutoff"]),
                  ie_gate=a.ie_gate or meta.get("ie_gate", "e6_e3b"))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2))
    print(f"{report['n_rows']:,} rows: precision {report['precision']}, recall {report['recall']}, "
          f"candidate recall {report['candidate_recall']} -> {a.out}")
    for line in report["slice_flags"]:
        print("  slice " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
