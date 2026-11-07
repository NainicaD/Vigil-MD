"""Query layer: fixed, parameterized BigQuery SQL that fetches the facts for an answer.

    from query_layer import BigQueryBackend, Budget, device_summary, narratives, identity
    backend = BigQueryBackend()                      # dataset from VIGIL_BQ_DATASET
    budget = Budget()                                # one per user question
    summary = device_summary(backend, "product_code", "LZG", budget)
"""
from .backend import BigQueryBackend, Budget, QueryBudgetExceeded
from .summary import device_summary, identity, narratives

__all__ = ["BigQueryBackend", "Budget", "QueryBudgetExceeded", "device_summary", "identity",
           "narratives"]
