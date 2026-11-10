"""Run s08_evaluate_hand.py for every model of one experiment (data/runs/<exp>/cutoffs.json).

Usage (from anywhere; extra arguments go to every s08 call):
    VIGIL_EXP=F1F2-text-contains python eval/s08_all.py
    python eval/s08_all.py --exp E4-ie-gate --ie-gate
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vigil_model.tracking import resolve_exp, run_outputs  # noqa: E402

args = sys.argv[1:]
exp = None
if "--exp" in args:                     # taken here and passed on, so s08 tags the same experiment
    exp = args[args.index("--exp") + 1]
scores_dir, cutoffs_path = run_outputs(exp)
if resolve_exp(exp) == "none":
    sys.exit("set VIGIL_EXP or pass --exp: without it this would score data/scores (the CI outputs)")
if not cutoffs_path.exists():
    sys.exit(f"{cutoffs_path} not found: run s06 for this experiment first")

for name, cutoff in json.load(open(cutoffs_path)).items():
    if cutoff is None:
        print(f"{name}: no 99%-precision cutoff on validation, skipped")
        continue
    track = "baseline" if name.startswith("logreg") else "xgb"
    cmd = [sys.executable, str(ROOT / "eval/s08_evaluate_hand.py"), "--scores",
           str(scores_dir / f"{name}_hand_scores.csv"), "--cutoff", str(cutoff), "--track", track, *args]
    print("+", " ".join(cmd[1:]), flush=True)
    subprocess.run(cmd, check=True)
