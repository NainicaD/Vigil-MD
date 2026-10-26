"""Great Expectations suites for the staging tables, generated from schemas.py.

What this module owns (agreed split with the anomaly checks):
  - schema: column names, types and modes, compared with the live table
    metadata (compare_schema; no table scan)
  - non-null and unique merge keys
  - allowed values, formats and date ranges (build_suite)

check_staging.py (anomaly detection) owns the comparisons with history:
null rates vs baseline, volume, category mix, raw-to-staged gap, and alerts.

Plain functions. build_suite needs great_expectations but no GCP connection.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.warehouse import schemas  # noqa: E402

# BigQuery table metadata reports legacy names for some standard SQL types
TYPE_ALIASES = {"INT64": "INTEGER", "FLOAT64": "FLOAT", "BOOL": "BOOLEAN"}

MIN_DATE = dt.date(1970, 1, 1)
PRODUCT_CODE_RE = r"^[A-Z]{3}$"

# Hand-written rules a schema cannot express.
# in_set / regex / lengths values: (rule, mostly). "mostly" is the share of
# non-null rows that must pass, set from the 2026-10-26 baselines with a margin.
# Allowed values are the values observed in the baselines; add new ones here
# when the FDA introduces them (the anomaly check warns on new categories).
RULES: dict[str, dict] = {
    "enforcement": {
        "in_set": {
            "recall_class": (["Class I", "Class II", "Class III"], 0.99),
            "status":       (["Terminated", "Ongoing", "Completed"], 0.99),
        },
        "dates": ["record_date"],
    },
    "recall": {
        "in_set": {
            "recall_status": (["Terminated", "Open, Classified", "Completed"], 0.99),
        },
        # baseline: 0.76% of product codes are not three capital letters
        "regex": {"product_code": (PRODUCT_CODE_RE, 0.98)},
        "dates": ["record_date"],
    },
    "maude": {
        # no baseline yet: loose limits until MAUDE is measured
        "regex":   {"product_code": (PRODUCT_CODE_RE, 0.95)},
        "lengths": {"udi_di": ((7, 23), 0.99)},
        "dates":   ["record_date"],
    },
    "gudid": {
        "in_set":  {"device_record_status": (["Published"], 0.99)},
        # baseline: UDI-DI lengths 7 to 23 (different issuing agencies)
        "lengths": {"udi_di": ((7, 23), 1.0)},
        "dates":   ["publish_date"],
    },
}


# ── Schema ────────────────────────────────────────────────────────────────────

def _norm_type(t: str) -> str:
    return TYPE_ALIASES.get(t.upper(), t.upper())


def expected_schema(source: str) -> list[tuple[str, str, str]]:
    """(name, type, mode) of the staging table, from schemas.py."""
    cols = schemas.FLAT_COLUMNS[source] + schemas.STAGING_EXTRA
    return [(n, _norm_type(t), m.upper()) for n, t, m in cols]


def compare_schema(expected: list[tuple[str, str, str]],
                   actual: list[tuple[str, str, str]]) -> list[str]:
    """Differences between the expected and the live schema. Empty list = match."""
    exp = {n: (_norm_type(t), m.upper()) for n, t, m in expected}
    act = {n: (_norm_type(t), m.upper()) for n, t, m in actual}
    problems = [f"missing column: {n}" for n in exp.keys() - act.keys()]
    problems += [f"unexpected column: {n}" for n in act.keys() - exp.keys()]
    for n in exp.keys() & act.keys():
        if exp[n][0] != act[n][0]:
            problems.append(f"type of {n}: expected {exp[n][0]}, found {act[n][0]}")
        if exp[n][1] != act[n][1]:
            problems.append(f"mode of {n}: expected {exp[n][1]}, found {act[n][1]}")
    return sorted(problems)


# ── Expectation suite ─────────────────────────────────────────────────────────

def columns_for_query(source: str) -> list[str]:
    """Columns the suite reads, in schema order (keeps the BigQuery scan small).

    REPEATED columns are never included: value expectations do not apply to arrays.
    """
    rules = RULES[source]
    wanted = set(schemas.MERGE_KEYS[source])
    for kind in ("in_set", "regex", "lengths"):
        wanted |= set(rules.get(kind, {}))
    wanted |= set(rules.get("dates", []))
    return [n for n, _, m in schemas.FLAT_COLUMNS[source] if n in wanted and m != "REPEATED"]


def build_suite(source: str, today: dt.date | None = None, context=None):
    """Expectation suite for one staging table (one ingest partition).

    GX 1.x needs an active data context to build a suite. Pass the runner's
    context; without one, a temporary in-memory (ephemeral) context is used.
    """
    import great_expectations as gx
    import great_expectations.expectations as gxe

    if context is None:
        context = gx.get_context(mode="ephemeral")

    today = today or dt.date.today()
    rules = RULES[source]
    keys = schemas.MERGE_KEYS[source]
    suite = gx.ExpectationSuite(name=f"{source}_stg")

    # Keys: never null, unique within the partition (staging de-duplicates per day)
    for k in keys:
        suite.add_expectation(gxe.ExpectColumnValuesToNotBeNull(column=k))
    if len(keys) == 1:
        suite.add_expectation(gxe.ExpectColumnValuesToBeUnique(column=keys[0]))
    else:
        suite.add_expectation(gxe.ExpectCompoundColumnsToBeUnique(column_list=keys))

    for col, (values, mostly) in rules.get("in_set", {}).items():
        suite.add_expectation(gxe.ExpectColumnValuesToBeInSet(
            column=col, value_set=values, mostly=mostly))
    for col, (regex, mostly) in rules.get("regex", {}).items():
        suite.add_expectation(gxe.ExpectColumnValuesToMatchRegex(
            column=col, regex=regex, mostly=mostly))
    for col, ((lo, hi), mostly) in rules.get("lengths", {}).items():
        suite.add_expectation(gxe.ExpectColumnValueLengthsToBeBetween(
            column=col, min_value=lo, max_value=hi, mostly=mostly))
    for col in rules.get("dates", []):
        # NULL dates are ignored here; their rate is an anomaly-check concern
        suite.add_expectation(gxe.ExpectColumnValuesToBeBetween(
            column=col, min_value=MIN_DATE, max_value=today + dt.timedelta(days=1)))
    return suite
