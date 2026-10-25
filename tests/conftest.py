import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def fake_gcp(monkeypatch):
    """Replace google.cloud.storage / bigquery so no test touches real GCP."""
    storage = MagicMock(name="storage")
    bigquery = MagicMock(name="bigquery")
    bigquery.SourceFormat.PARQUET = "PARQUET"
    bigquery.WriteDisposition.WRITE_TRUNCATE = "WRITE_TRUNCATE"

    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    cloud.bigquery = bigquery
    google = types.ModuleType("google")
    google.cloud = cloud
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)

    # BigQuery tables report 10 rows / 3 columns unless a test overrides it.
    table = bigquery.Client.return_value.get_table.return_value
    table.num_rows = 10
    table.schema = ["a", "b", "c"]
    return types.SimpleNamespace(storage=storage, bigquery=bigquery)
