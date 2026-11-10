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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


# Table metadata changes at most once a day (after the load), so its date is remembered briefly.
DATA_AS_OF_TTL_SECONDS = 300
MAX_PARALLEL = 8


def _parallel(fn, items: list) -> list:
    """fn over items at the same time (BigQuery round trips), results in the items' order."""
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(items))) as pool:
        return list(pool.map(fn, items))


class BigQueryBackend:
    def __init__(self, project: str = config.PROJECT, dataset: str = config.DATASET,
                 client=None, clock=time.monotonic):
        self.project, self.dataset = project, dataset
        self._client = client
        self._clock = clock
        self._as_of: dict[str, tuple[float, str | None]] = {}
        self._as_of_lock = threading.Lock()

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
        """Bytes each query would scan (dry runs, free; sent at the same time)."""
        from google.cloud import bigquery
        for q in queries:
            assert_read_only(q.sql)
        client = self.client

        def dry(q):
            job = client.query(q.sql, job_config=bigquery.QueryJobConfig(
                dry_run=True, use_query_cache=False, query_parameters=self._bq_params(q)))
            return int(job.total_bytes_processed or 0)
        return _parallel(dry, queries)

    def run(self, queries: list[Query], budget: Budget) -> tuple[list[list[dict]], list[QueryRecord]]:
        """Dry-run all, refuse above the budget, then run each with maximum_bytes_billed."""
        from google.cloud import bigquery
        estimates = self.estimate(queries)
        need_gb = sum(estimates) / 1e9
        if need_gb > budget.remaining_gb:
            raise QueryBudgetExceeded(
                f"{', '.join(q.name for q in queries)} would scan {need_gb:.2f} GB; "
                f"{budget.remaining_gb:.2f} GB left of {budget.max_gb:.2f} GB for this question")
        # The queries run at the same time. Each job may bill at most what is left once the
        # others' estimates are set aside, so together they stay within the budget, as when
        # they ran one after another. BigQuery bills at least 10 MB per job.
        left = budget.remaining_gb * 1e9
        client = self.client

        def one(item):
            q, est = item
            cap = max(10 * 1024 * 1024, math.ceil(left - (sum(estimates) - est)))
            job = client.query(q.sql, job_config=bigquery.QueryJobConfig(
                query_parameters=self._bq_params(q), maximum_bytes_billed=cap))
            rows = [{k: _plain(v) for k, v in dict(r).items()} for r in job.result()]
            billed = int(getattr(job, "total_bytes_billed", 0) or 0)
            return rows, QueryRecord(q.name, q.sql, {k: v for k, (_, v) in q.params.items()},
                                     q.tables, est, billed, len(rows))
        done = _parallel(one, list(zip(queries, estimates)))
        budget.spent_gb += sum(rec.billed_bytes for _, rec in done) / 1e9
        return [rows for rows, _ in done], [rec for _, rec in done]

    def data_as_of(self, tables: list[str]) -> dict[str, str]:
        """When each table was last modified (table metadata, no query cost). Remembered for
        DATA_AS_OF_TTL_SECONDS; the tables change at most once a day."""
        now, names = self._clock(), sorted(set(tables))
        with self._as_of_lock:
            missing = [n for n in names if n not in self._as_of
                       or now - self._as_of[n][0] >= DATA_AS_OF_TTL_SECONDS]
        client = self.client

        def fetch(name):
            t = client.get_table(f"{self.project}.{self.dataset}.{name}")
            return name, (t.modified.isoformat() if t.modified else None)
        fresh = _parallel(fetch, missing)
        with self._as_of_lock:
            for name, value in fresh:
                self._as_of[name] = (now, value)
            return {n: self._as_of[n][1] for n in names}
