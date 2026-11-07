"""Fixed, parameterized SQL for the chatbot. Pure functions: no GCP calls here.

Every value that comes from a user (the device key) is a bound query parameter (@key);
it is never pasted into the SQL text. Table names come from settings (config.DATASET), not
from users. Each query is a single read-only SELECT (assert_read_only).

The device key: product_code (FDA product code, e.g. LZG), brand (the brand name exactly as
in the data, case-insensitive), manufacturer (the normalized name, as manufacturer_name_norm),
udi_di (the device identifier). The device lookup decides the key; nothing here guesses.

Column meanings (vigil_md_prod): maude has one row per device per report, so reports are
counted with source_record_id once; record_date is the date FDA received the report.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import config
from .terms import NO_HARM, NO_INFORMATION

Table = Callable[[str], str]          # table name → `project.dataset.name`

# How each key type selects rows. Only these fixed strings are used; @key is bound.
MAUDE_FILTER = {
    "product_code": "m.product_code = UPPER(@key)",
    "brand":        "LOWER(m.device_name_raw) = LOWER(@key)",
    "manufacturer": "m.manufacturer_name_norm = LOWER(@key)",
    "udi_di":       "m.udi_di = @key",
}
# recall.device_name_raw is the product description, so a brand is matched inside it.
# There is no UDI in the recall data: a udi_di must first be turned into a brand or code.
RECALL_FILTER = {
    "product_code": "r.product_code = UPPER(@key)",
    "brand":        "STRPOS(LOWER(r.device_name_raw), LOWER(@key)) > 0",
    "manufacturer": "r.manufacturer_name_norm = LOWER(@key)",
}
GUDID_FILTER = {
    "product_code": "UPPER(@key) IN UNNEST(g.product_codes)",
    "brand":        "LOWER(g.device_name_raw) = LOWER(@key)",
    "manufacturer": "g.manufacturer_name_norm = LOWER(@key)",
    "udi_di":       "g.udi_di = @key",
}


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


def _check_key(key_type: str, key: str, allowed: dict) -> None:
    if key_type not in allowed:
        raise ValueError(f"key_type {key_type!r} not supported here; use one of {sorted(allowed)}")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key must be a non-empty string")


def _key(key: str) -> dict:
    return {"key": ("STRING", key.strip())}


def counts(tbl: Table, key_type: str, key: str) -> Query:
    """Reports per year and event type (each report counted once)."""
    _check_key(key_type, key, MAUDE_FILTER)
    sql = f"""\
SELECT EXTRACT(YEAR FROM record_date) AS year,
       IFNULL(event_type, '(none)') AS event_type,
       COUNT(*) AS reports
FROM (
  SELECT m.source_record_id, ANY_VALUE(m.event_type) AS event_type,
         MIN(m.record_date) AS record_date
  FROM {tbl('maude')} AS m
  WHERE {MAUDE_FILTER[key_type]}
  GROUP BY m.source_record_id
)
GROUP BY year, event_type
ORDER BY year, event_type"""
    return Query("counts", sql, _key(key), ("maude",))


def problems(tbl: Table, key_type: str, key: str, top_n: int = config.TOP_PROBLEMS) -> Query:
    """Top device and patient problem terms (no-information and no-harm terms left out),
    plus one row problem_type='no_harm' with the reports that say no patient harm."""
    _check_key(key_type, key, MAUDE_FILTER)
    sql = f"""\
WITH reports AS (
  SELECT DISTINCT m.source_record_id AS k
  FROM {tbl('maude')} AS m
  WHERE {MAUDE_FILTER[key_type]}
),
ranked AS (
  SELECT p.problem_type, p.problem_term, COUNT(DISTINCT p.mdr_report_key) AS reports
  FROM {tbl('maude_problems')} AS p
  JOIN reports ON reports.k = p.mdr_report_key
  WHERE p.problem_term NOT IN UNNEST(@no_information)
    AND p.problem_term NOT IN UNNEST(@no_harm)
  GROUP BY p.problem_type, p.problem_term
)
(SELECT problem_type, problem_term, reports
 FROM ranked
 WHERE TRUE
 QUALIFY ROW_NUMBER() OVER (PARTITION BY problem_type ORDER BY reports DESC, problem_term) <= @top_n)
UNION ALL
(SELECT 'no_harm' AS problem_type, CAST(NULL AS STRING) AS problem_term,
        COUNT(DISTINCT p.mdr_report_key) AS reports
 FROM {tbl('maude_problems')} AS p
 JOIN reports ON reports.k = p.mdr_report_key
 WHERE p.problem_type = 'patient' AND p.problem_term IN UNNEST(@no_harm))"""
    params = {**_key(key), "top_n": ("INT64", int(top_n)),
              "no_information": ("ARRAY<STRING>", list(NO_INFORMATION)),
              "no_harm": ("ARRAY<STRING>", list(NO_HARM))}
    return Query("problems", sql, params, ("maude", "maude_problems"))


def recalls(tbl: Table, key_type: str, key: str, limit: int = config.MAX_RECALLS) -> Query:
    """Recalls, newest first, with the FDA class; total_recalls on every row."""
    _check_key(key_type, key, RECALL_FILTER)
    sql = f"""\
SELECT r.source_record_id AS recall_number, e.recall_class, r.record_date,
       r.recall_status, r.device_name_raw AS product, r.manufacturer_name_raw AS firm,
       r.reason_text, r.root_cause, r.action,
       COUNT(*) OVER () AS total_recalls
FROM {tbl('recall')} AS r
LEFT JOIN {tbl('enforcement')} AS e ON e.source_record_id = r.source_record_id
WHERE {RECALL_FILTER[key_type]}
ORDER BY r.record_date DESC, r.source_record_id
LIMIT @limit"""
    return Query("recalls", sql, {**_key(key), "limit": ("INT64", int(limit))},
                 ("recall", "enforcement"))


def narrative_candidates(tbl: Table, key_type: str, key: str,
                         per_event_type: int = config.NARRATIVE_CANDIDATES,
                         event_types=config.NARRATIVE_EVENT_ORDER) -> Query:
    """Step 1 of narratives: the newest reports per event type (keys and dates only)."""
    _check_key(key_type, key, MAUDE_FILTER)
    sql = f"""\
SELECT source_record_id AS mdr_report_key, event_type, record_date
FROM (
  SELECT m.source_record_id, ANY_VALUE(m.event_type) AS event_type,
         MIN(m.record_date) AS record_date
  FROM {tbl('maude')} AS m
  WHERE {MAUDE_FILTER[key_type]} AND m.event_type IN UNNEST(@event_types)
  GROUP BY m.source_record_id
)
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY event_type ORDER BY record_date DESC, source_record_id DESC) <= @per_event_type"""
    params = {**_key(key), "event_types": ("ARRAY<STRING>", list(event_types)),
              "per_event_type": ("INT64", int(per_event_type))}
    return Query("narrative_candidates", sql, params, ("maude",))


def narrative_texts(tbl: Table, report_keys: list[str], dates: list[str],
                    max_chars: int = config.NARRATIVE_MAX_CHARS,
                    min_chars: int = config.NARRATIVE_MIN_CHARS) -> Query:
    """Step 2 of narratives: the texts of the chosen reports. maude_text is partitioned by
    month of date_received (_part_date), so filtering on the reports' dates reads only those
    months; the dry-run estimate reflects that (clustering alone would not)."""
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


def identity(tbl: Table, key_type: str, key: str, limit: int = 20) -> Query:
    """Who makes the device: GUDID records grouped by manufacturer, name, model, codes."""
    _check_key(key_type, key, GUDID_FILTER)
    sql = f"""\
SELECT g.manufacturer_name_raw AS manufacturer, g.device_name_raw AS device_name,
       g.model_number_raw AS model_number,
       ARRAY_TO_STRING(g.product_codes, ',') AS product_codes,
       COUNT(*) AS records, MIN(g.udi_di) AS example_udi_di
FROM {tbl('gudid')} AS g
WHERE {GUDID_FILTER[key_type]}
GROUP BY manufacturer, device_name, model_number, product_codes
ORDER BY records DESC, device_name
LIMIT @limit"""
    return Query("identity", sql, {**_key(key), "limit": ("INT64", int(limit))}, ("gudid",))
