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

# ── MAUDE event tables (problems and narrative text, for the chatbot) ─────────
# Built by scripts/warehouse/build_maude_events.py after the MAUDE merge.
# One RAW scan per ingest date fills the report-level staging table; the two
# prod tables are filled from it. mdr_report_key = maude.source_record_id.
MAUDE_EVENTS_STAGING_COLUMNS = [
    ("mdr_report_key",   "STRING",    "REQUIRED"),
    ("event_type",       "STRING",    "NULLABLE"),
    ("date_received",    "DATE",      "NULLABLE"),
    ("date_of_event",    "DATE",      "NULLABLE"),
    ("date_report",      "DATE",      "NULLABLE"),
    ("product_problems", "STRING",    "REPEATED"),   # distinct, sorted
    ("patient_problems", "STRING",    "REPEATED"),   # distinct over patient[], sorted
    ("mdr_text",         "JSON",      "NULLABLE"),   # the openFDA mdr_text array, as is
    ("record_hash",      "STRING",    "REQUIRED"),
    ("_ingest_date",     "DATE",      "REQUIRED"),
    ("_loaded_at",       "TIMESTAMP", "REQUIRED"),
]
MAUDE_EVENTS_STAGING_EXPIRY_DAYS = 14   # full text is large; prod keeps what is needed

MAUDE_PROBLEMS_COLUMNS = [
    ("mdr_report_key",  "STRING",    "REQUIRED"),
    ("problem_type",    "STRING",    "REQUIRED"),   # 'device' | 'patient'
    ("problem_term",    "STRING",    "REQUIRED"),
    ("event_type",      "STRING",    "NULLABLE"),
    ("date_received",   "DATE",      "NULLABLE"),
    ("_part_date",      "DATE",      "REQUIRED"),
    ("last_updated_at", "TIMESTAMP", "REQUIRED"),
]

MAUDE_TEXT_COLUMNS = [
    ("mdr_report_key",          "STRING",    "REQUIRED"),
    ("mdr_text_key",            "STRING",    "NULLABLE"),
    ("text_type_code",          "STRING",    "NULLABLE"),
    ("patient_sequence_number", "STRING",    "NULLABLE"),
    ("event_type",              "STRING",    "NULLABLE"),
    ("date_received",           "DATE",      "NULLABLE"),
    ("date_report",             "DATE",      "NULLABLE"),
    ("text",                    "STRING",    "REQUIRED"),
    ("_part_date",              "DATE",      "REQUIRED"),
    ("last_updated_at",         "TIMESTAMP", "REQUIRED"),
]

# Rows that must be unique in each prod table (checked after every write)
MAUDE_EVENT_KEYS = {
    "maude_problems": ["mdr_report_key", "problem_type", "problem_term"],
    "maude_text":     ["mdr_report_key", "mdr_text_key"],
}

# ── Daily summary tables (scripts/warehouse/build_summaries.py) ───────────────
# Each has a bucket column (summary_config.entity_bucket) and is partitioned by it: a lookup
# filters on bucket, entity_type and entity_id and reads one partition.
# Pre-computed facts per entity, rebuilt in full (CREATE OR REPLACE TABLE ... AS) once a day,
# so the chatbot and the website read a few rows instead of scanning maude. An entity is a
# device (GUDID records grouped by company and brand, reports via exact UDI links), a
# product_code, or a manufacturer (manufacturer_name_norm). Listed in build order.
SUMMARY_TABLES = {
    "device_catalog": [
        ("device_id", "STRING"), ("company_norm", "STRING"), ("brand_key", "STRING"),
        ("name", "STRING"), ("company", "STRING"), ("product_codes", "ARRAY<STRING>"),
        ("udi_dis", "ARRAY<STRING>"), ("models", "ARRAY<STRING>"), ("n_models", "INT64"),
        ("n_udis", "INT64"), ("bucket", "INT64"),
    ],
    "entity_reports": [
        ("entity_type", "STRING"), ("entity_id", "STRING"), ("source_record_id", "STRING"),
        ("event_type", "STRING"), ("record_date", "DATE"), ("bucket", "INT64"),
    ],
    "entity_counts": [
        ("entity_type", "STRING"), ("entity_id", "STRING"), ("year", "INT64"),
        ("event_type", "STRING"), ("reports", "INT64"), ("bucket", "INT64"),
    ],
    "entity_problems": [
        ("entity_type", "STRING"), ("entity_id", "STRING"), ("problem_type", "STRING"),
        ("problem_term", "STRING"), ("reports", "INT64"), ("rank", "INT64"), ("bucket", "INT64"),
    ],
    "entity_recalls": [
        ("entity_type", "STRING"), ("entity_id", "STRING"), ("matched_by", "STRING"),
        ("recall_number", "STRING"), ("recall_class", "STRING"), ("record_date", "DATE"),
        ("recall_status", "STRING"), ("product", "STRING"), ("firm", "STRING"),
        ("reason_text", "STRING"), ("root_cause", "STRING"), ("action", "STRING"),
        ("bucket", "INT64"),
    ],
    "entity_catalog": [
        ("entity_type", "STRING"), ("entity_id", "STRING"), ("title", "STRING"),
        ("subtitle", "STRING"), ("company_norm", "STRING"), ("product_codes", "ARRAY<STRING>"),
        ("top_names", "ARRAY<STRING>"), ("reports", "INT64"), ("deaths", "INT64"),
        ("injuries", "INT64"), ("malfunctions", "INT64"), ("bucket", "INT64"),
    ],
}
ENTITY_TYPES = ("device", "product_code", "manufacturer")
# The prod tables each summary reads (the build is skipped when none changed)
SUMMARY_SOURCES = ("gudid", "maude", "maude_problems", "recall", "enforcement")

# ── Links (the evidence-linked database: which records describe the same thing) ───────────
# One table per kind of link, each written only by its producer, so one rebuild never
# removes another's rows; the view LINKS_VIEW shows them together. The pipeline writes the
# exact links; the matching model's links (MAUDE to GUDID, method 'model' with a score) are
# planned as their own table in the same shape.
LINK_COLUMNS = [
    ("link_type", "STRING"),      # e.g. enforcement_recall
    ("left_source", "STRING"), ("left_id", "STRING"),
    ("right_source", "STRING"), ("right_id", "STRING"),
    ("link_key", "STRING"),       # what the link rests on, e.g. recall_number
    ("event_id", "STRING"),       # the recall event both records belong to, if any
    ("same_event", "BOOL"),       # both sides name the same event (a second check)
    ("method", "STRING"),         # exact_id now; model later
    ("score", "FLOAT64"),         # null for exact links
    ("match_kind", "STRING"),     # null for exact links; for model or UDI-agreement labels
    ("built_at", "TIMESTAMP"),
]
LINK_TABLES = {
    # link table → (left source, right source), prod tables it is built from
    "link_enforcement_recall": ("enforcement", "recall"),
}
LINKS_VIEW = "links"

# ── Table names ───────────────────────────────────────────────────────────────

def raw_table(project: str, source: str) -> str:
    return f"{project}.{DATASETS['raw']}.{source}_raw"


def staging_table(project: str, source: str) -> str:
    return f"{project}.{DATASETS['staging']}.{source}_stg"


def prod_table(project: str, source: str) -> str:
    return f"{project}.{DATASETS['prod']}.{source}"


def load_log_table(project: str) -> str:
    return f"{project}.{DATASETS['ops']}.load_log"


def summary_table(project: str, name: str, dataset: str | None = None) -> str:
    """A summary table in vigil_md_prod (or another dataset with the same tables, e.g. vigil_md_dev)."""
    if name not in SUMMARY_TABLES:
        raise ValueError(f"unknown summary table {name!r}")
    return f"{project}.{dataset or DATASETS['prod']}.{name}"


def link_table(project: str, name: str, dataset: str | None = None) -> str:
    """A link table in vigil_md_prod (or another dataset with the same tables, e.g. vigil_md_dev)."""
    if name not in LINK_TABLES and name != LINKS_VIEW:
        raise ValueError(f"unknown link table {name!r}")
    return f"{project}.{dataset or DATASETS['prod']}.{name}"


def maude_events_staging_table(project: str) -> str:
    return f"{project}.{DATASETS['staging']}.maude_events_stg"


def maude_event_prod_table(project: str, name: str) -> str:
    """name: 'maude_problems' or 'maude_text'."""
    if name not in MAUDE_EVENT_KEYS:
        raise ValueError(f"unknown MAUDE event table {name!r}")
    return f"{project}.{DATASETS['prod']}.{name}"


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
    specs.append({
        "table_id":  maude_events_staging_table(project),
        "columns":   MAUDE_EVENTS_STAGING_COLUMNS,
        "partition": ("_ingest_date", "DAY"),
        "cluster":   ["mdr_report_key"],
        "require_partition_filter": True,
        "partition_expiration_days": MAUDE_EVENTS_STAGING_EXPIRY_DAYS,
    })
    for name, columns in (("maude_problems", MAUDE_PROBLEMS_COLUMNS),
                          ("maude_text", MAUDE_TEXT_COLUMNS)):
        specs.append({
            "table_id":  maude_event_prod_table(project, name),
            "columns":   columns,
            "partition": ("_part_date", "MONTH"),   # same as prod maude
            "cluster":   ["mdr_report_key"],
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
