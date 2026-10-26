
from __future__ import annotations

# Rate checks need enough rows to mean something. Below this, a FAIL on a rate
# is reported as WARN (zero rows and row-loss checks are never downgraded).
DEFAULT_MIN_ROWS_FOR_PCT = 200

CHECKS: dict[str, dict] = {
    # Baseline measured 2026-10-26: 40,299 staged rows (40,300 raw).
    "enforcement": {
        "min_rows_for_pct": DEFAULT_MIN_ROWS_FOR_PCT,

        # Raw rows that did not reach staging (null keys dropped + duplicates).
        # Baseline gap: 1 row of 40,300.
        "gap": {"warn_pct": 0.5, "fail_pct": 2.0},

        # Staged rows against the previous load of the same source.
        "volume": {"warn_drop_pct": 20.0, "fail_drop_pct": 50.0},

        # Percent NULL or blank per column. Warn/fail when the rate rises by more
        # than this many points above the baseline.
        # source_record_id and link_key are left out on purpose: staging drops
        # rows with a NULL key, so they are always 0 and tell us nothing.
        "null_points": {"warn": 5.0, "fail": 10.0},
        "null_baseline_pct": {
            "event_id": 0.0,
            "manufacturer_name_raw": 0.0,
            "manufacturer_name_norm": 0.0,
            "device_name_raw": 0.0,
            "recall_class": 0.0,
            "reason_text": 0.12,
            "scope_text": 0.09,
            "status": 0.0,
            "distribution_pattern": 0.0,
            "record_date": 0.0,
        },

        # Dates. norm_date() turns unknown formats into NULL, so a rising NULL
        # rate in record_date is how a format change shows up.
        "date_column": "record_date",
        "bad_date_fail_pct": 1.0,          # future or pre-1970 dates

        # No product code column in enforcement.
        "product_code": None,

        # Categories seen in the baseline. A new value is a warning.
        "allowed_values": {
            "recall_class": ["Class I", "Class II", "Class III"],
            "status": ["Terminated", "Ongoing", "Completed"],
        },
        # Mix of the whole table at baseline. Only meaningful when one load holds
        # the full file (enforcement does, as far as the load log shows).
        "mix_baseline_pct": {
            "recall_class": {"Class II": 88.33, "Class I": 9.09, "Class III": 2.58},
            "status": {"Terminated": 62.87, "Ongoing": 35.59, "Completed": 1.55},
        },
        "mix_shift_warn_points": 10.0,

        # Longest allowed text per column. Fill in from the text length baseline
        # query (p99 and max), for example {"reason_text": 5000}. Empty = skipped.
        "text_max_length": {},
    },

    # Baseline measured 2026-10-26: 59,552 rows in raw, staging and prod.
    "recall": {
        "min_rows_for_pct": DEFAULT_MIN_ROWS_FOR_PCT,
        "gap": {"warn_pct": 0.5, "fail_pct": 2.0},
        "volume": {"warn_drop_pct": 20.0, "fail_drop_pct": 50.0},
        "null_points": {"warn": 5.0, "fail": 10.0},
        "null_baseline_pct": {
            "res_event_number": 0.0,
            "manufacturer_name_raw": 0.0,
            "manufacturer_name_norm": 0.0,
            "device_name_raw": 0.0,
            "product_code": 0.6,
            "reason_text": 0.08,
            "scope_text": 0.0,
            "recall_status": 0.0,
            "root_cause": 0.02,
            "action": 0.5,        # the screenshot cut this value off: confirm
            # event_date_posted is often empty in the FDA recall data (about 77%
            # coverage), so this baseline is high on purpose.
            "record_date": 22.8,
        },
        "date_column": "record_date",
        "bad_date_fail_pct": 1.0,
        # 454 of 59,552 codes (0.76%) are not three capital letters. That is close
        # to the guide's 1% fail limit, so the limits are relative to this baseline.
        "product_code": {"column": "product_code", "kind": "scalar",
                         "baseline_bad_pct": 0.76, "warn_points": 0.25, "fail_points": 1.0},
        "allowed_values": {
            "recall_status": ["Terminated", "Open, Classified", "Completed"],
        },
        "mix_baseline_pct": {
            "recall_status": {"Terminated": 75.04, "Open, Classified": 24.22, "Completed": 0.74},
        },
        "mix_shift_warn_points": 10.0,
        # reason_text: p99 604, max 2,127. scope_text is left out on purpose: its
        # max is 8,867,432 characters (p99 98,765), so a length limit says little.
        "text_max_length": {"reason_text": 5000},
    },

    # Baseline measured 2026-10-26: 5,238,573 rows in raw, staging and prod.
    # This table is large: look at the query cost estimate before the first run.
    "gudid": {
        "min_rows_for_pct": DEFAULT_MIN_ROWS_FOR_PCT,
        "gap": {"warn_pct": 0.5, "fail_pct": 2.0},
        "volume": {"warn_drop_pct": 20.0, "fail_drop_pct": 50.0},
        "null_points": {"warn": 5.0, "fail": 10.0},
        "null_baseline_pct": {
            "udi_di": 0.0,
            "manufacturer_name_raw": 0.0,
            "manufacturer_name_norm": 0.0,
            "device_name_raw": 0.0,
            "model_number_raw": 0.0,
            "catalog_number": 42.51,   # many devices have no catalog number
            "device_record_status": 0.0,
            "distribution_status": 0.0,
            "publish_date": 0.0,
        },
        "date_column": "publish_date",
        "bad_date_fail_pct": 1.0,
        # product_codes is an array: "missing" means an empty array (0.64%).
        # No UDI-DI format rule: GS1, HIBCC and ICCBBA formats differ (lengths 7 to
        # 23 seen, 89.8% are 14 digits).
        "product_code": {"column": "product_codes", "kind": "array",
                         "baseline_bad_pct": 0.0, "baseline_empty_pct": 0.64},
        # Every record is "Published" at baseline, so any other value is new.
        "allowed_values": {"device_record_status": ["Published"]},
        "mix_baseline_pct": {"device_record_status": {"Published": 100.0}},
        "mix_shift_warn_points": 10.0,
        "text_max_length": {},
    },

    # Baseline measured 2026-10-26: 21,499,653 reports in raw; 21,532,440 rows in
    # staging and PROD (one row per device). Loaded in ONE full load of quarter
    # shards (ingest_date 2026-10-26); later daily loads should hold only the
    # files that changed.
    "maude": {
        "min_rows_for_pct": DEFAULT_MIN_ROWS_FOR_PCT,

        # Staging makes one row per device (and one row for a report with none), so
        # raw rows are the wrong yardstick. This expression is summed over the raw
        # rows of the ingest date to get the rows staging should produce. At
        # baseline it equals the staged row count exactly (21,532,440).
        "expected_staged_expr": ("GREATEST(1, ARRAY_LENGTH("
                                 "IFNULL(JSON_QUERY_ARRAY(payload, '$.device'), [])))"),
        "gap": {"warn_pct": 0.5, "fail_pct": 2.0},

        # No volume check yet. The only previous load is the whole 21.5M-row table,
        # while a daily load holds just the changed quarter files, so comparing the
        # two would fail every day. A per-file comparison against load_log is still
        # to be built.
        "volume": None,

        # The baseline is the whole table, but a daily batch is one or a few
        # quarters whose rates can differ, so the limits are wider than elsewhere.
        # report_number has no baseline yet (cut off in the screenshot).
        "null_points": {"warn": 10.0, "fail": 20.0},
        "null_baseline_pct": {
            "manufacturer_name_raw": 0.47,
            "manufacturer_name_norm": 0.47,
            "device_name_raw": 0.36,
            "model_number_raw": 26.71,
            "catalog_number": 14.46,
            "lot_number": 43.8,
            "udi_di": 38.12,
            "product_code": 0.04,
            "event_type": 0.03,
            "adverse_event_flag": 0.2,
            "record_date": 0.0,
        },

        # Values such as N/A or dashes that are not NULL (share of all rows).
        "placeholder_points": {"warn": 3.0, "fail": 8.0},
        "placeholder_baseline_pct": {
            "manufacturer_name_raw": 0.06,
            "device_name_raw": 0.15,
            "model_number_raw": 3.84,
            "catalog_number": 1.37,
            "lot_number": 4.70,
            "udi_di": 0.0,
        },

        "date_column": "record_date",
        "bad_date_fail_pct": 1.0,
        # 456 of 21.5M codes (0.002%) are not three capital letters: mostly "---"
        # and "-", some lowercase codes.
        "product_code": {"column": "product_code", "kind": "scalar",
                         "baseline_bad_pct": 0.002, "warn_points": 0.01, "fail_points": 1.0},

        "allowed_values": {
            "event_type": ["Malfunction", "Injury", "Death", "Other", "No answer provided"],
            "adverse_event_flag": ["N", "Y"],
        },
        # Mix check off: a daily batch is a few quarters, not the whole table.
        "mix_baseline_pct": {},
        "mix_shift_warn_points": 10.0,
        "text_max_length": {},
    },
}


def get_config(source: str) -> dict | None:
    """Config for a source, or None when its baseline is not done yet."""
    return CHECKS.get(source)