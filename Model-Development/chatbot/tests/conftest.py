import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def mlflow_test_store(tmp_path_factory):
    """Every test logs to a temporary MLflow store, never to Model-Development/mlflow/."""
    root = tmp_path_factory.mktemp("mlflow")
    saved = {k: os.environ.get(k) for k in ("MLFLOW_TRACKING_URI", "VIGIL_MLFLOW_ARTIFACTS")}
    os.environ["MLFLOW_TRACKING_URI"] = f"sqlite:///{root / 'mlflow.db'}"
    os.environ["VIGIL_MLFLOW_ARTIFACTS"] = str(root / "artifacts")
    yield root
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
