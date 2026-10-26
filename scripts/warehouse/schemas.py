"""Table definitions for the RAW → STAGING → PROD warehouse.

Plain Python data only. No GCP imports. Every warehouse script reads
table names, columns, partitions and merge keys from here.

See docs/WAREHOUSE_PLAN.md ("BigQuery tables").
"""
from __future__ import annotations

LOCATION = "US"
SOURCES  = ["enforcement", "recall", "maude", "gudid"]

DATASETS = {
    "raw":     "vigil_md_raw",        # exists; holds *_sample tables too
    "staging": "vigil_md_staging",
    "prod":    "vigil_md_prod",
    "ops":     "vigil_md_ops",
}

# Existing tables that no warehouse script may touch (guardrail G12)
PROTECTED_DATASETS = {"vigil_md_processed"}
PROTECTED_SUFFIX   = "_sample"

# ── Columns ───────────────────────────────────────────────────────────────────
# (name, type, mode)

RAW_COLUMNS = [
    ("_ingest_date",      "DATE",      "REQUIRED"),
    ("_source_file",      "STRING",    "REQUIRED"),
    ("_source_partition", "STRING",    "NULLABLE"),
    ("_zip_sha256",       "STRING",    "NULLABLE"),
    ("_content_sha256",   "STRING",    "REQUIRED"),
    ("_loaded_at",        "TIMESTAMP", "REQUIRED"),
    ("payload",           "JSON",      "NULLABLE"),
]

# Business columns, ported from scripts/preprocessing/flatten_to_parquet.py.
# raw_json is dropped (raw.payload keeps the record).
FLAT_COLUMNS = {
    # flatten_enforcement, lines 76-93
    "enforcement": [
        ("source",                 "STRING", "NULLABLE"),
        ("source_record_id",       "STRING", "NULLABLE"),
        ("link_key",               "STRING", "NULLABLE"),
        ("event_id",               "STRING", "NULLABLE"),
        ("manufacturer_name_raw",  "STRING", "NULLABLE"),
        ("manufacturer_name_norm", "STRING", "NULLABLE"),
        ("device_name_raw",        "STRING", "NULLABLE"),
        ("recall_class",           "STRING", "NULLABLE"),
        ("reason_text",            "STRING", "NULLABLE"),
        ("scope_text",             "STRING", "NULLABLE"),
        ("record_date",            "DATE",   "NULLABLE"),
        ("status",                 "STRING", "NULLABLE"),
        ("distribution_pattern",   "STRING", "NULLABLE"),
    ],
    # flatten_recall, lines 102-118
    "recall": [
        ("source",                 "STRING", "NULLABLE"),
        ("source_record_id",       "STRING", "NULLABLE"),
        ("link_key",               "STRING", "NULLABLE"),
        ("res_event_number",       "STRING", "NULLABLE"),
        ("manufacturer_name_raw",  "STRING", "NULLABLE"),
        ("manufacturer_name_norm", "STRING", "NULLABLE"),
        ("device_name_raw",        "STRING", "NULLABLE"),
        ("product_code",           "STRING", "NULLABLE"),
        ("reason_text",            "STRING", "NULLABLE"),
        ("scope_text",             "STRING", "NULLABLE"),
        ("record_date",            "DATE",   "NULLABLE"),
        ("recall_status",          "STRING", "NULLABLE"),
        ("root_cause",             "STRING", "NULLABLE"),
        ("action",                 "STRING", "NULLABLE"),
    ],
    # flatten_maude, lines 133-151, plus device_seq (UNNEST ... WITH OFFSET)
    "maude": [
        ("source",                 "STRING", "NULLABLE"),
        ("source_record_id",       "STRING", "NULLABLE"),
        ("device_seq",             "INT64",  "NULLABLE"),
        ("manufacturer_name_raw",  "STRING", "NULLABLE"),
        ("manufacturer_name_norm", "STRING", "NULLABLE"),
        ("device_name_raw",        "STRING", "NULLABLE"),
        ("model_number_raw",       "STRING", "NULLABLE"),
        ("catalog_number",         "STRING", "NULLABLE"),
        ("lot_number",             "STRING", "NULLABLE"),
        ("udi_di",                 "STRING", "NULLABLE"),
        ("product_code",           "STRING", "NULLABLE"),
        ("record_date",            "DATE",   "NULLABLE"),
        ("event_type",             "STRING", "NULLABLE"),
        ("adverse_event_flag",     "STRING", "NULLABLE"),
        ("report_number",          "STRING", "NULLABLE"),
    ],
    # flatten_gudid, lines 173-187, mapped to the bulk XML fields (Step 5):
    # source_record_id = publicDeviceRecordKey, udi_di = Primary deviceId.
    "gudid": [
        ("source",                 "STRING", "NULLABLE"),
        ("source_record_id",       "STRING", "NULLABLE"),
        ("udi_di",                 "STRING", "NULLABLE"),
        ("manufacturer_name_raw",  "STRING", "NULLABLE"),
        ("manufacturer_name_norm", "STRING", "NULLABLE"),
        ("device_name_raw",        "STRING", "NULLABLE"),
        ("model_number_raw",       "STRING", "NULLABLE"),
        ("catalog_number",         "STRING", "NULLABLE"),
        ("product_codes",          "STRING", "REPEATED"),
        ("device_record_status",   "STRING", "NULLABLE"),
        ("publish_date",           "DATE",   "NULLABLE"),
        ("distribution_status",    "STRING", "NULLABLE"),
    ],
}

MERGE_KEYS = {
    "enforcement": ["source_record_id"],
    "recall":      ["source_record_id"],
    "maude":       ["source_record_id", "device_seq"],
    "gudid":       ["source_record_id"],   # publicDeviceRecordKey (user decision, Step 5)
}

# Prod tables partitioned by _part_date = COALESCE(record_date, DATE '1900-01-01').
# GUDID has no stable date (publish_date changes), so it has no date partition.
DATE_PARTITIONED_PROD = {"enforcement", "recall", "maude"}
PART_DATE_SENTINEL    = "1900-01-01"

STAGING_EXTRA = [
    ("record_hash",  "STRING",    "REQUIRED"),
    ("_ingest_date", "DATE",      "REQUIRED"),
    ("_loaded_at",   "TIMESTAMP", "REQUIRED"),
]

PROD_EXTRA = [
    ("record_hash",     "STRING",    "REQUIRED"),
    ("first_loaded_at", "TIMESTAMP", "REQUIRED"),
    ("last_updated_at", "TIMESTAMP", "REQUIRED"),
]

LOAD_LOG_COLUMNS = [
    ("source",         "STRING",    "REQUIRED"),
    ("source_file",    "STRING",    "REQUIRED"),
    ("zip_sha256",     "STRING",    "NULLABLE"),
    ("content_sha256", "STRING",    "REQUIRED"),
    ("ingest_date",    "DATE",      "REQUIRED"),
    ("row_count",      "INT64",     "REQUIRED"),
    ("mode",           "STRING",    "REQUIRED"),
    ("loaded_at",      "TIMESTAMP", "REQUIRED"),
]

# ── Table names ───────────────────────────────────────────────────────────────

def raw_table(project: str, source: str) -> str:
    return f"{project}.{DATASETS['raw']}.{source}_raw"


def staging_table(project: str, source: str) -> str:
    return f"{project}.{DATASETS['staging']}.{source}_stg"


def prod_table(project: str, source: str) -> str:
    return f"{project}.{DATASETS['prod']}.{source}"


def load_log_table(project: str) -> str:
    return f"{project}.{DATASETS['ops']}.load_log"


# ── Table specs ───────────────────────────────────────────────────────────────
# partition: (field, "DAY"|"MONTH") or None
# require_partition_filter: queries without a partition filter fail (pruning guard)

def table_specs(project: str) -> list[dict]:
    """Return one spec dict per warehouse table. Pure function."""
    specs = []
    for s in SOURCES:
        specs.append({
            "table_id":  raw_table(project, s),
            "columns":   RAW_COLUMNS,
            "partition": ("_ingest_date", "DAY"),
            "cluster":   ["_source_file"],
            "require_partition_filter": True,
        })
    for s in SOURCES:
        specs.append({
            "table_id":  staging_table(project, s),
            "columns":   FLAT_COLUMNS[s] + STAGING_EXTRA,
            "partition": ("_ingest_date", "DAY"),
            "cluster":   MERGE_KEYS[s],
            "require_partition_filter": True,
        })
    for s in SOURCES:
        columns = FLAT_COLUMNS[s] + PROD_EXTRA
        partition = None
        if s in DATE_PARTITIONED_PROD:
            columns = columns + [("_part_date", "DATE", "REQUIRED")]
            # MONTH, not DAY: recall dates span decades and DAY could pass
            # the BigQuery partition-count limit.
            partition = ("_part_date", "MONTH")
        specs.append({
            "table_id":  prod_table(project, s),
            "columns":   columns,
            "partition": partition,
            "cluster":   MERGE_KEYS[s],
            "require_partition_filter": False,
        })
    specs.append({
        "table_id":  load_log_table(project),
        "columns":   LOAD_LOG_COLUMNS,
        "partition": None,
        "cluster":   ["source"],
        "require_partition_filter": False,
    })
    for spec in specs:
        assert_not_protected(spec["table_id"])
    return specs


def assert_not_protected(table_id: str) -> None:
    """Raise if a table id points at an existing table we must not touch."""
    _, dataset, table = table_id.split(".")
    if dataset in PROTECTED_DATASETS or table.endswith(PROTECTED_SUFFIX):
        raise ValueError(f"Refusing protected table: {table_id}")
