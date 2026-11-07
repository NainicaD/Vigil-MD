"""Agreement between two hand labelers (Day 1, N6). Offline: reads two CSVs, no GCP.

Joins the first labeler's sheet and the second labeler's copy on pair_id, keeps the rows both labeled,
and reports Cohen's kappa on `label`, overall and per hard_case_reason (E3b, E4 and E9 are the ones
SAME_DEVICE_DEFINITION.md section 6.6 expects to split opinions). Every disagreement is written out
for the review meeting.

Outputs:
  reports/hand_labels/agreement_v<batch>.json            counts, kappa, confusion matrix per group
  reports/hand_labels/disagreements_v<batch>.csv         one row per pair the two labelers disagree on

Exit codes: 0 ok, 2 a file or column is missing, 3 a data problem (a label outside the allowed set,
a pair_id twice in one file, or no pair labeled by both).

Usage (from Model-Development/):
  $PY scripts/hand_label_agreement.py \
      --first labels/hand_label_sample_v1.csv --second labels/hand_label_sample_v1.labeler2.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "hand_labels"
LABELS = ("MATCH", "NON_MATCH", "INSUFFICIENT_EVIDENCE")
FOCUS_REASONS = ("E3b_prefix_only", "E4_non_distinctive", "E9_kit_wording")
REQUIRED = ("pair_id", "maude_row_id", "cand_rank", "label", "hard_case_reason")
EXIT_OK, EXIT_CONFIG, EXIT_DATA = 0, 2, 3

log = logging.getLogger("hand_label_agreement")


class DataError(ValueError):
    """The labels themselves are wrong (exit 3)."""


def read_labels(path: Path) -> dict[str, dict]:
    """pair_id -> row, for rows with a non-empty label. Raises DataError on a bad label or a repeat."""
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise KeyError(f"{path.name} is missing columns {missing}")
        rows = list(reader)
    seen = Counter(r["pair_id"] for r in rows)
    repeated = [k for k, n in seen.items() if n > 1]
    if repeated:
        raise DataError(f"{path.name}: pair_id appears more than once: {repeated[:5]}")
    out = {}
    for r in rows:
        label = r["label"].strip()
        if not label:
            continue
        if label not in LABELS:
            raise DataError(f"{path.name}: pair {r['pair_id']} has label {label!r}; allowed: {LABELS}")
        out[r["pair_id"]] = {**r, "label": label}
    return out


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Kappa for two raters on the same items. None when there are no items. When chance agreement is
    1 (both raters used one and the same label everywhere) kappa is defined as 1.0."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(first[k] * second[k] for k in LABELS) / (n * n)
    if expected == 1:
        return 1.0
    return (observed - expected) / (1 - expected)


def summarize(pairs: list[tuple[str, str]]) -> dict:
    kappa = cohen_kappa(pairs)
    return {
        "n": len(pairs),
        "agree": sum(a == b for a, b in pairs),
        "percent_agree": round(100 * sum(a == b for a, b in pairs) / len(pairs), 1) if pairs else None,
        "kappa": None if kappa is None else round(kappa, 3),
        "confusion": {f"{a}|{b}": n for (a, b), n in sorted(Counter(pairs).items())},
    }


def compare(first: dict[str, dict], second: dict[str, dict]) -> tuple[dict, list[dict]]:
    both = sorted(set(first) & set(second))
    if not both:
        raise DataError("no pair_id was labeled by both labelers")
    pairs = {pid: (first[pid]["label"], second[pid]["label"]) for pid in both}
    groups: dict[str, list] = {}
    for pid in both:
        reason = first[pid].get("hard_case_reason") or "none"
        groups.setdefault(reason, []).append(pairs[pid])
    report = {
        "overall": summarize(list(pairs.values())),
        "focus_E3b_E4_E9": summarize([p for r in FOCUS_REASONS for p in groups.get(r, [])]),
        "by_hard_case_reason": {r: summarize(ps) for r, ps in sorted(groups.items())},
        "only_first": len(set(first) - set(second)),
        "only_second": len(set(second) - set(first)),
    }
    disagreements = [{
        "pair_id": pid, "maude_row_id": first[pid]["maude_row_id"], "cand_rank": first[pid]["cand_rank"],
        "hard_case_reason": first[pid].get("hard_case_reason", ""),
        "label_first": pairs[pid][0], "label_second": pairs[pid][1],
        "rule_first": first[pid].get("rule_cited", ""), "rule_second": second[pid].get("rule_cited", ""),
        "notes_first": first[pid].get("notes", ""), "notes_second": second[pid].get("notes", ""),
    } for pid in both if pairs[pid][0] != pairs[pid][1]]
    disagreements.sort(key=lambda d: (d["maude_row_id"], int(d["cand_rank"] or 0)))
    return report, disagreements


def main(argv=None, out_dir: Path = OUT_DIR) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--first", type=Path, required=True, help="the main labeled sheet")
    ap.add_argument("--second", type=Path, required=True, help="the second labeler's copy")
    ap.add_argument("--batch", type=int, default=1)
    args = ap.parse_args(argv)
    try:
        first, second = read_labels(args.first), read_labels(args.second)
        report, disagreements = compare(first, second)
    except (OSError, KeyError) as e:
        log.error("%s", e)
        return EXIT_CONFIG
    except DataError as e:
        log.error("%s", e)
        return EXIT_DATA

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"agreement_v{args.batch}.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    cols = ["pair_id", "maude_row_id", "cand_rank", "hard_case_reason", "label_first", "label_second",
            "rule_first", "rule_second", "notes_first", "notes_second"]
    with (out_dir / f"disagreements_v{args.batch}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(disagreements)
    o, focus = report["overall"], report["focus_E3b_E4_E9"]
    log.info("overall: %d pairs, %s%% agree, kappa %s", o["n"], o["percent_agree"], o["kappa"])
    log.info("E3b + E4 + E9: %d pairs, %s%% agree, kappa %s", focus["n"], focus["percent_agree"], focus["kappa"])
    log.info("%d disagreements written to %s", len(disagreements), out_dir)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
