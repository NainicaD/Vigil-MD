import sys
import types
from unittest.mock import MagicMock

import pytest

# imports resolve via pythonpath = ["."] in pyproject.toml, not a sys.path hack


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
