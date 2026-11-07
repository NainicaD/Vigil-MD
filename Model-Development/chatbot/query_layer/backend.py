"""Run the query layer's SQL on BigQuery, with a cost guard per question.

A Budget is made for each user question (config.MAX_GB_PER_QUESTION). Before any query
runs, every query of the step is dry-run (free); if together they would scan more than the
budget has left, nothing runs (QueryBudgetExceeded). Each job also carries
maximum_bytes_billed, so BigQuery itself refuses to bill more. What each query read and
billed is recorded for the answer's sources.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from . import config
from .queries import Query, assert_read_only


class QueryBudgetExceeded(RuntimeError):
    """The dry run says this step would scan more than the question's budget allows."""


@dataclass
class Budget:
    max_gb: float = config.MAX_GB_PER_QUESTION
    spent_gb: float = 0.0

    @property
    def remaining_gb(self) -> float:
        return max(0.0, self.max_gb - self.spent_gb)


@dataclass
class QueryRecord:
    """What one query was and cost: the answer cites these, the gate re-checks with them."""
    name: str
    sql: str
    params: dict
    tables: tuple[str, ...]
    estimated_bytes: int
    billed_bytes: int = 0
    rows: int = 0
    extra: dict = field(default_factory=dict)


def _plain(value):
    """BigQuery row values → JSON-friendly (dates as ISO strings)."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


class BigQueryBackend:
    def __init__(self, project: str = config.PROJECT, dataset: str = config.DATASET,
                 client=None):
        self.project, self.dataset = project, dataset
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery
            self._client = bigquery.Client(project=self.project)
        return self._client

    def table(self, name: str) -> str:
        return f"`{self.project}.{self.dataset}.{name}`"

    @staticmethod
    def _bq_params(query: Query) -> list:
        from google.cloud import bigquery
        out = []
        for name, (typ, value) in query.params.items():
            if typ.startswith("ARRAY<"):
                out.append(bigquery.ArrayQueryParameter(name, typ[6:-1], value))
            else:
                out.append(bigquery.ScalarQueryParameter(name, typ, value))
        return out

    def estimate(self, queries: list[Query]) -> list[int]:
        """Bytes each query would scan (dry run, free)."""
        from google.cloud import bigquery
        out = []
        for q in queries:
            assert_read_only(q.sql)
            job = self.client.query(q.sql, job_config=bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, query_parameters=self._bq_params(q)))
            out.append(int(job.total_bytes_processed or 0))
        return out

    def run(self, queries: list[Query], budget: Budget) -> tuple[list[list[dict]], list[QueryRecord]]:
        """Dry-run all, refuse above the budget, then run each with maximum_bytes_billed."""
        from google.cloud import bigquery
        estimates = self.estimate(queries)
        need_gb = sum(estimates) / 1e9
        if need_gb > budget.remaining_gb:
            raise QueryBudgetExceeded(
                f"{', '.join(q.name for q in queries)} would scan {need_gb:.2f} GB; "
                f"{budget.remaining_gb:.2f} GB left of {budget.max_gb:.2f} GB for this question")
        results, records = [], []
        for q, est in zip(queries, estimates):
            cap = max(10 * 1024 * 1024, math.ceil(budget.remaining_gb * 1e9))  # BigQuery bills >= 10 MB
            job = self.client.query(q.sql, job_config=bigquery.QueryJobConfig(
                query_parameters=self._bq_params(q), maximum_bytes_billed=cap))
            rows = [{k: _plain(v) for k, v in dict(r).items()} for r in job.result()]
            billed = int(getattr(job, "total_bytes_billed", 0) or 0)
            budget.spent_gb += billed / 1e9
            results.append(rows)
            records.append(QueryRecord(q.name, q.sql, {k: v for k, (_, v) in q.params.items()},
                                       q.tables, est, billed, len(rows)))
        return results, records

    def data_as_of(self, tables: list[str]) -> dict[str, str]:
        """When each table was last modified (table metadata, no query cost)."""
        out = {}
        for name in sorted(set(tables)):
            t = self.client.get_table(f"{self.project}.{self.dataset}.{name}")
            out[name] = t.modified.isoformat() if t.modified else None
        return out
