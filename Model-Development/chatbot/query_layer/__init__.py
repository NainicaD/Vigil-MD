"""Query layer: fixed, parameterized BigQuery SQL on the daily summary tables.

    from query_layer import BigQueryBackend, Budget, device_summary, narratives, identity
    backend = BigQueryBackend()                      # dataset from VIGIL_BQ_DATASET
    budget = Budget()                                # one per user question
    summary = device_summary(backend, "device", "dev:f0cdc15b4d63", budget)
    # entity types: device (id from device_search), product_code, manufacturer
"""
from .backend import BigQueryBackend, Budget, QueryBudgetExceeded
from .summary import device_summary, identity, narratives

__all__ = ["BigQueryBackend", "Budget", "QueryBudgetExceeded", "device_summary", "identity",
           "narratives"]
