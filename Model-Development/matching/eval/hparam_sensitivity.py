"""Hyperparameter sensitivity from s06's random search (Guideline 5).

Reads <scores dir>/trials.csv (one row per trial: model, the 6 XGBoost hyperparameters and the
validation average precision that picked the trial) and plots that AP against each hyperparameter,
one panel per parameter, one colour per model. Only the best trial is scored on the hand sheet, so
hand precision per trial does not exist; validation AP is what the search optimises.
Writes hparam_sensitivity.png and prints each parameter's Spearman correlation with the AP.

Usage (from Model-Development/matching):
    python eval/hparam_sensitivity.py --scores-dir data/runs/final_v1_hparam/scores
"""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PARAMS = ["n_estimators", "max_depth", "learning_rate", "subsample", "colsample_bytree", "min_child_weight"]
AP = "val_select_average_precision"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scores-dir", type=Path, default=ROOT / "data/scores")
    a = p.parse_args(argv)
    trials = pd.read_csv(a.scores_dir / "trials.csv")
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), sharey=True)
    for ax, param in zip(axes.flat, PARAMS):
        for model, g in trials.groupby("model"):
            ax.scatter(g[param], g[AP], label=model, s=18, alpha=0.75)
        ax.set_xlabel(param)
    for ax in axes[:, 0]:
        ax.set_ylabel("validation average precision")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle(f"Random search, {len(trials)} trials ({a.scores_dir.parent.name})")
    fig.tight_layout()
    fig.savefig(a.scores_dir / "hparam_sensitivity.png", dpi=120)
    corr = trials.groupby("model")[[*PARAMS, AP]].corr(method="spearman")[AP].unstack()[PARAMS]
    print("Spearman correlation of each hyperparameter with validation AP:")
    print(corr.round(2).to_string())
    print(f"spread of validation AP per model:\n{trials.groupby('model')[AP].agg(['min', 'max']).round(4)}")
    print(f"wrote {a.scores_dir / 'hparam_sensitivity.png'}")


if __name__ == "__main__":
    main()
