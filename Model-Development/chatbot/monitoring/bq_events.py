"""Reads monitoring events from BigQuery, where the log sink copies them.

The sink vigil-md-monitoring-events writes the Cloud Run service's stdout events
(api/events.py) into vigil_md_monitoring.run_googleapis_com_stdout (one table per log, day
partitions on `timestamp`). Each row's jsonPayload is the event.

Cost guard: a dry run first (free); above max_gb nothing runs (BudgetExceeded); the query also
carries maximum_bytes_billed. The table holds a few MB a month, so a window costs fractions of
a cent.
"""
from __future__ import annotations

import datetime as dt

PROJECT = "vigil-md-1792696816"
DATASET = "vigil_md_monitoring"
TABLE = "run_googleapis_com_stdout"          # the sink's table for Cloud Run stdout
DEFAULT_MAX_GB = 0.1


class BudgetExceeded(RuntimeError):
    """The dry run says the query would scan more than max_gb."""


class NoEventsTable(RuntimeError):
    """The sink has written nothing yet (no event since it was created)."""


def window_sql(project: str = PROJECT, dataset: str = DATASET, table: str = TABLE) -> str:
    """Events of the last @days days (day partitions pruned by the timestamp filter)."""
    return (f"SELECT jsonPayload AS event FROM `{project}.{dataset}.{table}` "
            f"WHERE timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) "
            f"AND jsonPayload.event = 'router_decision'")


def _plain(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def read_window(days: int, client=None, project: str = PROJECT, dataset: str = DATASET,
                table: str = TABLE, max_gb: float = DEFAULT_MAX_GB) -> tuple[list[dict], dict]:
    """(events, info) for the last `days` days. info: estimated and billed GB.

    Raises NoEventsTable if the sink has not created its table yet, BudgetExceeded above max_gb.
    """
    from google.api_core import exceptions
    from google.cloud import bigquery
    client = client or bigquery.Client(project=project)
    sql = window_sql(project, dataset, table)
    params = [bigquery.ScalarQueryParameter("days", "INT64", int(days))]
    try:
        dry = client.query(sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params))
    except exceptions.NotFound as e:
        raise NoEventsTable(f"{project}.{dataset}.{table} does not exist yet: no event has "
                            f"been written since the sink was created") from e
    est_gb = int(dry.total_bytes_processed or 0) / 1e9
    if est_gb > max_gb:
        raise BudgetExceeded(f"reading {days} days of events would scan {est_gb:.3f} GB "
                             f"(cap {max_gb} GB)")
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params, maximum_bytes_billed=max(10 * 1024 * 1024,
                                                          int(max_gb * 1e9))))
    events = [_plain(dict(row["event"])) for row in job.result() if row["event"]]
    billed = int(getattr(job, "total_bytes_billed", 0) or 0) / 1e9
    return events, {"estimated_gb": round(est_gb, 6), "billed_gb": round(billed, 6),
                    "table": f"{project}.{dataset}.{table}", "days": days}
