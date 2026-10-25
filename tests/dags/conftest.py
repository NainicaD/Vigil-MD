"""Shared setup for DAG tests: isolated Airflow home + a fake HTTP layer.

Skipped automatically where Airflow is not installed. Nothing here talks to
the network or to GCP.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("airflow")

ROOT = Path(__file__).resolve().parents[2]
DAG_FILE = ROOT / "dags" / "pull_samples_dag.py"

# Must be set before airflow is imported anywhere.
_HOME = tempfile.mkdtemp(prefix="airflow-test-")
os.environ["AIRFLOW_HOME"] = _HOME
os.environ["AIRFLOW__CORE__LOAD_EXAMPLES"] = "False"
os.environ["AIRFLOW__CORE__UNIT_TEST_MODE"] = "True"
os.environ["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = f"sqlite:///{_HOME}/airflow.db"


@pytest.fixture(scope="session", autouse=True)
def airflow_db():
    from airflow.utils import db
    db.resetdb()


@pytest.fixture
def dag_module(tmp_path, monkeypatch):
    """The DAG file imported as a module, with OUT_DIR redirected to tmp_path."""
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("pull_samples_dag_under_test", DAG_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path / "samples")
    return mod


class FakeFDA:
    """Stands in for requests.Session.get. Records every call."""

    def __init__(self, n_records=3, fail_for=()):
        self.n = n_records
        self.fail_for = fail_for
        self.calls = []

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {})))
        resp = MagicMock()
        if any(tag in url for tag in self.fail_for):
            resp.status_code = 500
            resp.raise_for_status.side_effect = RuntimeError("500 server error")
            return resp
        resp.status_code = 200
        resp.raise_for_status.return_value = None
        if "api.fda.gov" in url:
            take = min(params.get("limit", self.n), self.n)
            resp.json.return_value = {"results": [
                {"id": i, "device": [{"udi_di": f"UDI{i}"}]} for i in range(take)]}
        else:  # GUDID lookup
            resp.json.return_value = {"gudid": {"device": {"deviceRecordStatus": "Published"}}}
        return resp


@pytest.fixture
def fake_http(monkeypatch):
    import requests
    import time
    fake = FakeFDA()
    monkeypatch.setattr(requests.Session, "get", fake)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    return fake
