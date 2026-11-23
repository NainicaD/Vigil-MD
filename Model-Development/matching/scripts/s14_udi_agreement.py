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

Limit: rows with a UDI are easier than the pool (rows without one). Read these numbers as a
trend between runs and model versions, not as the accuracy on the pool.

File in, file out: no BigQuery.

Usage (from anywhere):
    python scripts/s14_udi_agreement.py --candidates <dir>/candidates.parquet \
        --links <dir>/links.parquet --bundle models/matching-same-device-v1.1.0 --out <dir>/check_report.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def agreement(cands: pd.DataFrame, links: pd.DataFrame) -> dict:
    """The check's numbers from the candidates and the MATCH links of one udi_check run. Pure."""
    if cands["true_group_key"].isna().all():
        raise SystemExit("no true_group_key: these candidates are not from s13 --mode udi_check")
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
            "n_pairs": len(cands), "n_links": len(links), "n_links_right": len(correct),
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
    a = ap.parse_args(argv)

    report = agreement(pd.read_parquet(a.candidates), pd.read_parquet(a.links))
    meta = json.loads((a.bundle / "meta.json").read_text())
    report.update(bundle_version=meta.get("version", "unknown"), cutoff=float(meta["cutoff"]),
                  ie_gate=a.ie_gate or meta.get("ie_gate", "e6_e3b"))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(report, indent=2))
    print(f"{report['n_rows']:,} rows: precision {report['precision']}, recall {report['recall']}, "
          f"candidate recall {report['candidate_recall']} -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
