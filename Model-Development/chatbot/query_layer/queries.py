"""Fixed, parameterized SQL for the chatbot, on the daily summary tables. Pure functions.

Every lookup filters on bucket, entity_type and entity_id, so BigQuery reads one partition
(the tables are partitioned by bucket; config.entity_bucket). The entity type and id are
bound query parameters, never pasted into the SQL text; table names come from settings.
Each query is a single read-only SELECT (assert_read_only).

The summary tables (built daily by Data-Pipeline/scripts/warehouse/build_summaries.py):
  entity_counts    entity × year × event type → reports
  entity_problems  entity × top problem terms (no-information terms already left out),
                   plus one 'no_harm' row
  entity_recalls   entity × recall, with class, reason, root cause, action
  entity_catalog   entity: title, company, product codes, top brand names, totals
  entity_reports   entity × report (for narratives)
  device_catalog   device: name, company, product codes, model numbers, UDIs
Device reports are exact UDI links (MAUDE udi_di = one of the device's GUDID DIs).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import config

Table = Callable[[str], str]          # table name → `project.dataset.name`
ENTITY_FILTER = "bucket = @bucket AND entity_type = @entity_type AND entity_id = @entity_id"


@dataclass(frozen=True)
class Query:
    """One SQL statement and its bound parameters: {name: (BigQuery type, value)}."""
    name: str
    sql: str
    params: dict[str, tuple[str, object]] = field(default_factory=dict)
    tables: tuple[str, ...] = ()


_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|export|load|call|"
    r"declare|set|begin|commit|rollback|execute)\b", re.IGNORECASE)


def assert_read_only(sql: str) -> None:
    """Raise unless sql is one SELECT/WITH statement with no write keyword."""
    body = re.sub(r"--[^\n]*", "", sql).strip().rstrip(";")
    if ";" in body:
        raise ValueError("more than one statement")
    if not re.match(r"(?is)^\(?\s*(select|with)\b", body):
        raise ValueError("not a SELECT")
    hit = _WRITE_WORDS.search(re.sub(r"'[^']*'", "''", body))
    if hit:
        raise ValueError(f"write keyword not allowed: {hit.group(0)}")


def entity_params(entity_type: str, entity_id: str) -> dict:
    """The bound parameters that select one entity's partition and rows."""
    if entity_type not in config.ENTITY_TYPES:
        raise ValueError(f"entity_type must be one of {config.ENTITY_TYPES}, got {entity_type!r}")
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise ValueError("entity_id must be a non-empty string")
    entity_id = entity_id.strip()
    return {"bucket": ("INT64", config.entity_bucket(entity_type, entity_id)),
            "entity_type": ("STRING", entity_type), "entity_id": ("STRING", entity_id)}


def counts(tbl: Table, entity_type: str, entity_id: str) -> Query:
    """Reports per year and event type."""
    sql = f"""\
SELECT year, event_type, reports
FROM {tbl('entity_counts')}
WHERE {ENTITY_FILTER}
ORDER BY year, event_type"""
    return Query("counts", sql, entity_params(entity_type, entity_id), ("entity_counts",))


def problems(tbl: Table, entity_type: str, entity_id: str, top_n: int = config.TOP_PROBLEMS) -> Query:
    """Top device and patient problem terms, plus the 'no_harm' row."""
    sql = f"""\
SELECT problem_type, problem_term, reports
FROM {tbl('entity_problems')}
WHERE {ENTITY_FILTER} AND (problem_type = 'no_harm' OR rank <= @top_n)
ORDER BY problem_type, rank"""
    return Query("problems", sql, {**entity_params(entity_type, entity_id),
                                   "top_n": ("INT64", int(top_n))}, ("entity_problems",))


def recalls(tbl: Table, entity_type: str, entity_id: str, limit: int = config.MAX_RECALLS) -> Query:
    """Recalls, newest first, with the FDA class; total_recalls on every row."""
    sql = f"""\
SELECT recall_number, recall_class, record_date, recall_status, product, firm,
       reason_text, root_cause, action, matched_by,
       COUNT(*) OVER () AS total_recalls
FROM {tbl('entity_recalls')}
WHERE {ENTITY_FILTER}
ORDER BY record_date DESC, recall_number
LIMIT @limit"""
    return Query("recalls", sql, {**entity_params(entity_type, entity_id),
                                  "limit": ("INT64", int(limit))}, ("entity_recalls",))


def catalog(tbl: Table, entity_type: str, entity_id: str) -> Query:
    """The entity's title, company, product codes, top names and totals."""
    sql = f"""\
SELECT entity_type, entity_id, title, subtitle, company_norm, product_codes, top_names,
       reports, deaths, injuries, malfunctions
FROM {tbl('entity_catalog')}
WHERE {ENTITY_FILTER}"""
    return Query("catalog", sql, entity_params(entity_type, entity_id), ("entity_catalog",))


def device_details(tbl: Table, device_id: str) -> Query:
    """Who makes a device and its model numbers (GUDID)."""
    p = entity_params("device", device_id)
    sql = f"""\
SELECT device_id, name, company, product_codes, models, n_models, n_udis
FROM {tbl('device_catalog')}
WHERE bucket = @bucket AND device_id = @entity_id"""
    return Query("device_details", sql, {"bucket": p["bucket"], "entity_id": p["entity_id"]},
                 ("device_catalog",))


def narrative_candidates(tbl: Table, entity_type: str, entity_id: str,
                         per_event_type: int = config.NARRATIVE_CANDIDATES,
                         event_types=config.NARRATIVE_EVENT_ORDER) -> Query:
    """Step 1 of narratives: the newest reports per event type (keys and dates only)."""
    sql = f"""\
SELECT source_record_id AS mdr_report_key, event_type, record_date
FROM {tbl('entity_reports')}
WHERE {ENTITY_FILTER} AND event_type IN UNNEST(@event_types)
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY event_type ORDER BY record_date DESC, source_record_id DESC) <= @per_event_type"""
    params = {**entity_params(entity_type, entity_id),
              "event_types": ("ARRAY<STRING>", list(event_types)),
              "per_event_type": ("INT64", int(per_event_type))}
    return Query("narrative_candidates", sql, params, ("entity_reports",))


def narrative_texts(tbl: Table, report_keys: list[str], dates: list[str],
                    max_chars: int = config.NARRATIVE_MAX_CHARS,
                    min_chars: int = config.NARRATIVE_MIN_CHARS) -> Query:
    """Step 2 of narratives: the texts of the chosen reports. maude_text is partitioned by
    month of date_received (_part_date) and clustered by report, so filtering on the reports'
    dates and keys reads only those blocks (the dry run reflects it)."""
    sql = f"""\
SELECT t.mdr_report_key, t.mdr_text_key, t.event_type, t.date_received,
       LENGTH(t.text) AS text_length,
       SUBSTR(t.text, 1, @max_chars + 1) AS text
FROM {tbl('maude_text')} AS t
WHERE t._part_date IN UNNEST(@dates)
  AND t.mdr_report_key IN UNNEST(@report_keys)
  AND LENGTH(t.text) >= @min_chars"""
    params = {"report_keys": ("ARRAY<STRING>", list(report_keys)),
              "dates": ("ARRAY<DATE>", list(dates)),
              "max_chars": ("INT64", int(max_chars)), "min_chars": ("INT64", int(min_chars))}
    return Query("narrative_texts", sql, params, ("maude_text",))
