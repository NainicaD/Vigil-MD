"""Model gate: a new matching model replaces the champion only if it is not worse (Guidelines 7.2, 7.3, 7.6).

Reads, from one scores dir (data/scores in CI, data/runs/<exp>/scores locally):
  <model>_hand.json     eval/s08_evaluate_hand.py --out-json (hand-sheet metrics and slices)
  train_metrics.json    scripts/s06_train_models.py (test-split metrics per model)
and compares them with ci/champion_metrics.json (committed) under the rules in ci/gate.json:
  floors     always fail when broken
  ceilings   fail when broken; only warn if the champion already broke it and the candidate is no worse
  tolerance  not worse than the champion by more than this
Bias: bias_max_gap_precision / bias_max_gap_recall are the widest best-minus-worst gap between the
strat_model_usable and strat_mfr_size slices, counting only slices with at least MIN_N accepted
pairs (precision) or true matches (recall). Small slices are listed in the report but not gated.

Writes gate.json (verdict and every check, read by s11 before publishing), bias_report.md and
bias_slices.png to --out-dir. Exit 0: candidate passes. 1: the champion stays (the rollback).
2: missing input. --report-only always exits 0 (smoke data). --write-champion makes the candidate
the new reference and exits 0.

Usage (from Model-Development/matching):
    python ci/gate.py --scores-dir data/runs/final_v1/scores --candidate xgb_no_match_kind
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SLICE_COLS = ["strat_model_usable", "strat_mfr_size"]
MIN_N = 8


def slices(metrics: dict) -> dict[str, dict[str, dict]]:
    """{col: {value: {precision, recall, n, accepted, matches}}} from s08's flat slice_* keys."""
    out = {}
    for col in SLICE_COLS:
        prefix = f"slice_{col}_"
        for key in metrics:
            if key.startswith(prefix) and key.endswith("_n"):
                value = key[len(prefix):-len("_n")]
                out.setdefault(col, {})[value] = {
                    k: metrics.get(f"{prefix}{value}_{k}") for k in ("precision", "recall", "n", "accepted", "matches")}
    return out


def bias_gaps(sl: dict) -> dict:
    """Widest gap per metric over all slice columns; None when no column has two big-enough slices."""
    gaps = {}
    for metric, count in (("precision", "accepted"), ("recall", "matches")):
        widest = None
        for values in sl.values():
            vals = [s[metric] for s in values.values() if s[metric] is not None and (s[count] or 0) >= MIN_N]
            if len(vals) >= 2:
                widest = max(widest or 0.0, max(vals) - min(vals))
        gaps[f"bias_max_gap_{metric}"] = widest
    return gaps


def candidate_metrics(scores_dir: Path, model: str) -> dict:
    hand = json.loads((scores_dir / f"{model}_hand.json").read_text())
    train = json.loads((scores_dir / "train_metrics.json").read_text()).get(model, {})
    metrics = {**hand["metrics"], **{k: v for k, v in train.items() if v is not None}}
    metrics.update(bias_gaps(slices(hand["metrics"])))
    return {"model_name": model, "ie_gate": hand["ie_gate"], "cutoff": hand["cutoff"], "metrics": metrics}


def decide(candidate: dict, champion: dict, rules: dict) -> tuple[bool, list]:
    """The chatbot's gate rules (Model-Development/chatbot/query_router/gate.py) without the gain rule.
    Returns (passed, [(name, PASS|FAIL|WARN, detail)])."""
    def status(ok, soft=False):
        return "PASS" if ok else ("WARN" if soft else "FAIL")

    cand, champ = candidate["metrics"], champion["metrics"]
    lower = set(rules["lower_is_better"])
    checks = [("same IE gate", status(candidate["ie_gate"] == champion["ie_gate"]),
               f'{candidate["ie_gate"]} vs {champion["ie_gate"]}')]
    for name, floor in rules["floors"].items():
        value = cand.get(name)
        checks.append((f"{name} >= {floor}", status(value is not None and value >= floor), f"{value}"))
    for name, ceiling in rules["ceilings"].items():
        value, old = cand.get(name), champ.get(name)
        ok = value is None or value <= ceiling     # no slice big enough: no gap
        narrows = not ok and old is not None and old > ceiling and value <= old
        detail = f"{value}" + (f" (champion {old})" if narrows else "")
        checks.append((f"{name} <= {ceiling}", status(ok, narrows), detail))
    for name, tol in rules["tolerance"].items():
        new, old = cand.get(name), champ.get(name)
        if new is None or old is None:
            continue
        worse = (new - old) if name in lower else (old - new)
        checks.append((f"{name} not worse by > {tol}", status(worse <= tol + 1e-12), f"{new} vs champion {old}"))
    return all(s != "FAIL" for _, s, _ in checks), checks


def bias_report(model: str, sl: dict, gaps: dict) -> str:
    lines = [f"# Bias report: {model}", "",
             f"Hand sheet, candidate rows. Gaps count only slices with >= {MIN_N} accepted pairs "
             "(precision) or true matches (recall).", ""]
    for col, values in sl.items():
        lines += [f"## {col}", "", "| slice | n | matches | accepted | precision | recall |", "|---|---|---|---|---|---|"]
        for value, s in values.items():
            fmt = lambda v: "-" if v is None else f"{v:.3f}"  # noqa: E731
            lines.append(f"| {value} | {s['n']:.0f} | {s['matches'] or 0:.0f} | {s['accepted'] or 0:.0f} "
                         f"| {fmt(s['precision'])} | {fmt(s['recall'])} |")
        lines.append("")
    lines += ["## Widest gaps", ""] + [f"- {k}: {'-' if v is None else f'{v:.3f}'}" for k, v in gaps.items()]
    return "\n".join(lines) + "\n"


def bias_chart(sl: dict, path: Path) -> bool:
    """Precision and recall per slice, one panel per slice column. False if matplotlib is missing."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    fig, axes = plt.subplots(1, max(len(sl), 1), figsize=(5 * max(len(sl), 1), 3.5), squeeze=False)
    for ax, (col, values) in zip(axes[0], sl.items()):
        names = list(values)
        x = range(len(names))
        for off, metric in ((-0.2, "precision"), (0.2, "recall")):
            ax.bar([i + off for i in x], [values[n][metric] or 0 for n in names], width=0.4, label=metric)
        ax.set_xticks(list(x), [f"{n}\n(n={values[n]['n']:.0f})" for n in names])
        ax.set_ylim(0, 1.05)
        ax.set_title(col)
        ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Compare a new matching model with the champion")
    p.add_argument("--scores-dir", type=Path, default=ROOT / "data/scores")
    p.add_argument("--candidate", default="xgb_no_match_kind", help="model name (the released recipe)")
    p.add_argument("--champion", type=Path, default=ROOT / "ci/champion_metrics.json")
    p.add_argument("--rules", type=Path, default=ROOT / "ci/gate.json")
    p.add_argument("--out-dir", type=Path, help="default: --scores-dir")
    p.add_argument("--summary", type=Path, help="append the checks as Markdown (GITHUB_STEP_SUMMARY)")
    p.add_argument("--report-only", action="store_true", help="always exit 0 (smoke data)")
    p.add_argument("--write-champion", action="store_true", help="make the candidate the reference, exit 0")
    p.add_argument("--source", default="", help="with --write-champion: where the numbers come from")
    a = p.parse_args(argv)
    out_dir = a.out_dir or a.scores_dir

    try:
        cand = candidate_metrics(a.scores_dir, a.candidate)
        rules = json.loads(a.rules.read_text())
        champ = None if a.write_champion else json.loads(a.champion.read_text())
    except FileNotFoundError as e:
        print(f"gate: missing input: {e.filename}", file=sys.stderr)
        return 2

    if a.write_champion:
        cand.update({"source": a.source or str(a.scores_dir), "written": date.today().isoformat()})
        a.champion.write_text(json.dumps(cand, indent=2) + "\n")
        print(f"wrote {a.champion}")
        return 0

    passed, checks = decide(cand, champ, rules)
    sl = slices(cand["metrics"])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "bias_report.md").write_text(
        bias_report(a.candidate, sl, {k: cand["metrics"][k] for k in cand["metrics"] if k.startswith("bias_")}))
    bias_chart(sl, out_dir / "bias_slices.png")
    (out_dir / "gate.json").write_text(json.dumps({
        "passed": passed, "report_only": a.report_only, "candidate": a.candidate,
        "champion": champ.get("source", str(a.champion)),
        "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks],
        "metrics": cand["metrics"]}, indent=2) + "\n")

    print(f"gate: {a.candidate} vs champion ({champ.get('source', a.champion)})")
    for name, s, detail in checks:
        print(f"  {s:<4}  {name}: {detail}")
    print("result: candidate passes" if passed else "result: champion stays")
    if a.summary:
        rows = "\n".join(f"| {s} | {n} | {d} |" for n, s, d in checks)
        with open(a.summary, "a") as f:
            f.write(f"\n### Model gate: {'passes' if passed else 'champion stays'}"
                    f"{' (report only)' if a.report_only else ''}\n\n| | check | value |\n|---|---|---|\n{rows}\n")
    return 0 if passed or a.report_only else 1


if __name__ == "__main__":
    sys.exit(main())
