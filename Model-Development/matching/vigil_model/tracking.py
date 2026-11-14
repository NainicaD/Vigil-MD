"""One MLflow store and one set of lineage tags for every run (s06, s08, s09, s10, diag, embedding).

Every run carries `exp` (the experiment id from docs/EXPERIMENTS.md, set with VIGIL_EXP or --exp),
the git commit, the label-set manifest hash and its caps_and_ratios. Compare an experiment with
    mlflow.search_runs(experiment_names=["vigil-md-linkage"], filter_string="tags.exp = 'E1-hard-neg'")
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tracking_uri() -> str:
    return os.environ.get("MLFLOW_TRACKING_URI") or f"sqlite:///{ROOT / 'mlflow.db'}"


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def resolve_exp(exp: str | None = None) -> str:
    """--exp, else $VIGIL_EXP, else 'none'. The id becomes a folder name, so it is kept to a safe charset."""
    exp = exp or os.environ.get("VIGIL_EXP") or "none"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", exp) or exp in (".", ".."):
        raise ValueError(f"experiment id {exp!r}: use letters, digits, '.', '_' or '-'")
    return exp


def run_outputs(exp: str | None = None) -> tuple[Path, Path]:
    """(scores dir, cutoffs.json) of one experiment: data/runs/<exp>/, so runs never overwrite each other.
    With no experiment it is the old data/scores + data/cutoffs.json, which CI reads."""
    exp = resolve_exp(exp)
    if exp == "none":
        return ROOT / "data" / "scores", ROOT / "data" / "cutoffs.json"
    run = ROOT / "data" / "runs" / exp
    return run / "scores", run / "cutoffs.json"


def lineage_tags(manifest_path: str | Path | None, exp: str | None = None) -> dict[str, str]:
    """Tags that say which experiment, code and label set made a run. A missing manifest gives 'unknown'."""
    path = Path(manifest_path) if manifest_path else None
    found = path is not None and path.exists()
    tags = {"exp": resolve_exp(exp),
            "git_commit": git_commit(),
            "label_set_manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if found else "unknown"}
    caps = json.loads(path.read_text()).get("caps_and_ratios", {}) if found else {}
    tags.update({f"caps_{k}": str(v) for k, v in caps.items()})
    return tags
