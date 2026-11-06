import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def fake_bigquery(monkeypatch):
    """Replace google.cloud.bigquery so no test touches real GCP."""
    bigquery = MagicMock(name="bigquery")
    cloud = types.ModuleType("google.cloud")
    cloud.bigquery = bigquery
    google = types.ModuleType("google")
    google.cloud = cloud
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)
    return bigquery
