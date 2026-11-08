"""Check that a training run left complete, sane outputs, and summarize them (CI, after s06).

Fails with exit 3 (a data problem: no retry) when:
  - data/cutoffs.json is missing, not JSON, or names none of the expected models
  - a model in cutoffs.json has no data/scores/<model>_hand_scores.csv
  - a scores file does not have one row per hand-sheet row (same pair_id set)
  - a score is missing, not a number, or outside [0, 1]
  - with --require-mlflow: mlflow.db is missing, or a model has no finished MLflow run
Exit 2 means a bad argument or an unreadable input path.

It writes a Markdown summary (each model's 99%-precision cutoff, and the metrics s06 logged) to
--summary, e.g. $GITHUB_STEP_SUMMARY, so the numbers show on the workflow run page. It reads
mlflow.db with sqlite3 from the standard library, so it needs no MLflow install. It never fails on
a metric value: whether a model is good enough is decided in the validation step, not here.

Usage (from Model-Development/):
    python ci/check_training_outputs.py --hand data/hand_features.csv --summary "$GITHUB_STEP_SUMMARY"
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_MODELS = ("logreg_v1", "xgb_all", "xgb_no_match_kind", "xgb_no_kind_derived")
SHOWN_METRICS = ("val_average_precision", "val_recall_at_p99", "cutoff_p99", "test_average_precision",
                 "test_precision", "test_recall")
EXIT_OK, EXIT_CONFIG, EXIT_DATA = 0, 2, 3


class OutputError(ValueError):
    """The training outputs are incomplete or wrong (exit 3)."""


def read_cutoffs(path: Path) -> dict:
    if not path.exists():
        raise OutputError(f"{path} is missing: s06 did not finish")
    try:
        cutoffs = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise OutputError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(cutoffs, dict) or not set(cutoffs) & set(EXPECTED_MODELS):
        raise OutputError(f"{path} names none of {EXPECTED_MODELS}: {cutoffs!r}")
    missing = [m for m in EXPECTED_MODELS if m not in cutoffs]
    if missing:
        raise OutputError(f"{path} is missing models {missing}")
    return cutoffs


def read_pair_ids(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as f:
        return [r["pair_id"] for r in csv.DictReader(f)]


def check_scores(scores_dir: Path, models, hand_ids: list[str]) -> dict[str, int]:
    """Rows per model. Raises OutputError on a missing file, a row mismatch or a bad score."""
    rows = {}
    want = sorted(hand_ids)
    for m in models:
        path = scores_dir / f"{m}_hand_scores.csv"
        if not path.exists():
            raise OutputError(f"{m}: {path} is missing")
        with path.open(newline="", encoding="utf-8") as f:
            data = list(csv.DictReader(f))
        if sorted(r["pair_id"] for r in data) != want:
            raise OutputError(f"{m}: {len(data)} scored rows do not match the {len(want)} hand-sheet rows")
        for r in data:
            try:
                s = float(r["score"])
            except (TypeError, ValueError):
                raise OutputError(f"{m}: pair {r['pair_id']} has score {r['score']!r}") from None
            if math.isnan(s) or not 0.0 <= s <= 1.0:
                raise OutputError(f"{m}: pair {r['pair_id']} has score {s} outside [0, 1]")
        rows[m] = len(data)
    return rows


def read_mlflow(db: Path) -> dict[str, dict[str, float]]:
    """Latest finished run per run name -> its latest metrics. Empty if the file has no runs table."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        runs = con.execute(
            "SELECT run_uuid, name FROM runs WHERE status = 'FINISHED' AND lifecycle_stage = 'active' "
            "ORDER BY start_time").fetchall()
        latest = {name: uuid for uuid, name in runs}            # later runs overwrite earlier ones
        out = {}
        for name, uuid in latest.items():
            metrics = con.execute("SELECT key, value FROM latest_metrics WHERE run_uuid = ?", (uuid,)).fetchall()
            out[name] = {k: v for k, v in metrics}
        return out
    except sqlite3.DatabaseError:
        return {}
    finally:
        con.close()


def summary_md(cutoffs: dict, rows: dict, metrics: dict, title: str) -> str:
    def fmt(v):
        return "-" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
    head = ["model", "cutoff (99% precision)", "hand rows scored", *SHOWN_METRICS]
    lines = [f"### {title}", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for m in EXPECTED_MODELS:
        got = metrics.get(m, {})
        lines.append("| " + " | ".join([m, fmt(cutoffs.get(m)), fmt(rows.get(m)),
                                       *[fmt(got.get(k)) for k in SHOWN_METRICS]]) + " |")
    hand = [(m, metrics[f"{m}_hand"]) for m in EXPECTED_MODELS if f"{m}_hand" in metrics]
    if hand:
        lines += ["", "**Hand labels (s08, the honest test).** Every metric s08 logged, except per-slice ones:", "",
                  "| model | metrics |", "|---|---|"]
        for m, got in hand:
            shown = ", ".join(f"{k}={fmt(v)}" for k, v in sorted(got.items()) if not k.startswith("slice_"))
            lines.append(f"| {m} | {shown or '-'} |")
    no_cut = [m for m in EXPECTED_MODELS if cutoffs.get(m) is None]
    lines += ["", f"Models that never reached 99% precision on validation: {', '.join(no_cut) or 'none'}."]
    if not metrics:
        lines.append("MLflow metrics were not available for this run.")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cutoffs", type=Path, default=ROOT / "data" / "cutoffs.json")
    p.add_argument("--scores-dir", type=Path, default=ROOT / "data" / "scores")
    p.add_argument("--hand", type=Path, default=ROOT / "data" / "hand_features.csv")
    p.add_argument("--mlflow-db", type=Path, default=ROOT / "mlflow.db")
    p.add_argument("--require-mlflow", action="store_true", help="fail if a model has no finished MLflow run")
    p.add_argument("--summary", type=Path, help="append a Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    p.add_argument("--title", default="Training run")
    args = p.parse_args(argv)
    try:
        hand_ids = read_pair_ids(args.hand)
    except (OSError, KeyError) as e:
        print(f"ERROR cannot read the hand features {args.hand}: {e}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cutoffs = read_cutoffs(args.cutoffs)
        rows = check_scores(args.scores_dir, EXPECTED_MODELS, hand_ids)
        metrics = read_mlflow(args.mlflow_db) if args.mlflow_db.exists() else {}
        if args.require_mlflow:
            if not args.mlflow_db.exists():
                raise OutputError(f"{args.mlflow_db} is missing")
            missing = [m for m in EXPECTED_MODELS if m not in metrics]
            if missing:
                raise OutputError(f"no finished MLflow run for {missing}")
    except OutputError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return EXIT_DATA
    text = summary_md(cutoffs, rows, metrics, args.title)
    print(text)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(text)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
