"""MLflow tracking, following the shared conventions in Model-Development/MLFLOW.md.

One store per machine: Model-Development/mlflow/ (SQLite database + artifacts folder, not
in git). MLFLOW_TRACKING_URI (and VIGIL_MLFLOW_ARTIFACTS for the artifacts folder) override
it, so a shared team server later needs no code change.

Every run carries REQUIRED_TAGS (see required_tags()).
"""
from __future__ import annotations

import getpass
import hashlib
import os
import subprocess
from pathlib import Path

MODEL_DEV = Path(__file__).resolve().parents[2]          # Model-Development/
DEFAULT_STORE = MODEL_DEV / "mlflow"
WORKSTREAM = "chatbot"
EXPERIMENT = "chatbot/question-classifier"
REGISTERED_MODEL = "chatbot-question-classifier"
REQUIRED_TAGS = ("workstream", "git_commit", "git_dirty", "data_version", "author")


def tracking_uri() -> str:
    return os.environ.get("MLFLOW_TRACKING_URI") or f"sqlite:///{DEFAULT_STORE / 'mlflow.db'}"


def artifact_root() -> Path:
    return Path(os.environ.get("VIGIL_MLFLOW_ARTIFACTS") or DEFAULT_STORE / "artifacts")


def setup(experiment: str = EXPERIMENT):
    """Point MLflow at the store and select the experiment (created with its artifact
    folder under artifact_root(), so artifacts never land in the working directory).
    Returns the mlflow module."""
    import mlflow
    if tracking_uri().startswith("sqlite:///"):
        Path(tracking_uri().removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(tracking_uri())
    exp = mlflow.get_experiment_by_name(experiment)
    if exp is None:
        folder = artifact_root() / experiment.replace("/", "__")
        folder.mkdir(parents=True, exist_ok=True)
        exp_id = mlflow.create_experiment(experiment, artifact_location=folder.as_uri())
    else:
        exp_id = exp.experiment_id
    mlflow.set_experiment(experiment_id=exp_id)
    return mlflow


def git_info(repo: Path = MODEL_DEV) -> dict[str, str]:
    """git_commit and git_dirty (uncommitted changes), read-only git calls."""
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                              check=True).stdout.strip()
    try:
        return {"git_commit": run("rev-parse", "HEAD"),
                "git_dirty": str(bool(run("status", "--porcelain"))).lower()}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": "unknown", "git_dirty": "unknown"}


def data_version(paths: list[Path]) -> str:
    """Short SHA-256 over the files' names and contents: same data, same version."""
    h = hashlib.sha256()
    for p in sorted(Path(x) for x in paths):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def required_tags(data_paths: list[Path], workstream: str = WORKSTREAM) -> dict[str, str]:
    return {"workstream": workstream, **git_info(), "data_version": data_version(data_paths),
            "author": os.environ.get("VIGIL_AUTHOR") or getpass.getuser()}
