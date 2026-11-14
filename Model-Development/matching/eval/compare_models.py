"""One chart comparing every model of a run on the scorecard metrics (Guideline 4).

Reads <scores dir>/<model>_hand.json (eval/s08_all.py) and train_metrics.json (s06); writes
comparison.png and comparison.csv to the same folder.

Usage (from Model-Development/matching):
    python eval/compare_models.py --scores-dir data/runs/final_v1_hparam/scores
"""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# (label, source, key): the scorecard in docs/EXPERIMENTS.md
METRICS = [("hand precision", "hand", "all_precision"), ("hand recall", "hand", "all_recall"),
           ("IE accept rate", "hand", "ie_accept_rate"), ("NON_MATCH accept rate", "hand", "non_match_accept_rate"),
           ("test precision", "train", "test_precision"), ("test recall", "train", "test_recall")]


def table(scores_dir: Path) -> pd.DataFrame:
    train = json.loads((scores_dir / "train_metrics.json").read_text())
    rows = {}
    for path in sorted(scores_dir.glob("*_hand.json")):
        hand = json.loads(path.read_text())
        name = hand["model_name"]
        src = {"hand": hand["metrics"], "train": train.get(name, {})}
        rows[name] = {label: src[s].get(k) for label, s, k in METRICS}
    return pd.DataFrame(rows).T


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scores-dir", type=Path, default=ROOT / "data/scores")
    a = p.parse_args(argv)
    df = table(a.scores_dir)
    df.to_csv(a.scores_dir / "comparison.csv")
    ax = df.T.plot.bar(figsize=(10, 4.5), width=0.8, rot=0)
    ax.set_ylim(0, 1.05)
    ax.set_title(f"Models on the scorecard metrics ({a.scores_dir.parent.name})")
    ax.legend(loc="center left", bbox_to_anchor=(1, 0.5))
    plt.tight_layout()
    plt.savefig(a.scores_dir / "comparison.png", dpi=120)
    print(df.round(3).to_string())
    print(f"wrote {a.scores_dir / 'comparison.png'}")


if __name__ == "__main__":
    main()
