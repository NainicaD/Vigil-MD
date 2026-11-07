"""Settings for the daily summary tables (scripts/warehouse/build_summaries.py).

The term lists come from the chatbot data checks of 2026-11-07: about 7% of patient terms and
about 10% of the top device terms carry no information, and about half of all patient terms
say that no harm was reported. NO_INFORMATION terms are left out of the ranking; NO_HARM terms
too, but their report count is kept in one row (problem_type 'no_harm').
"""

NO_INFORMATION = (
    "Adverse Event Without Identified Device or Use Problem",
    "Insufficient Device Problem Information",
    "Appropriate Device Problem Term/Code Not Available",
    "Insufficient Information",
    "No Code Available",
    "No Information",
    "Appropriate Clinical Signs, Symptoms and Conditions Term/Code Not Available",
)
NO_HARM = (
    "No Clinical Signs, Symptoms or Conditions",
    "No Consequences Or Impact To Patient",
    "No Known Impact Or Consequence To Patient",
    "No Patient Involvement",
)

TOP_TERMS = 25            # problem terms kept per entity and problem type
TOP_MODELS = 20           # model numbers kept per device
TOP_NAMES = 5             # most reported brand names kept per product code
MIN_BRAND_CHARS = 4       # a device name shorter than this never matches a recall's text
MAX_GB = 30.0             # refuse a build whose statements add up to more than this

# Summary tables are partitioned by a bucket of the entity, so a lookup of one entity reads one
# partition. Clustering alone is not enough: BigQuery checks maximum_bytes_billed against the
# whole table for a clustered-only filter (a 3 GB entity_reports lookup was refused at 200 MB).
BUCKETS = 1000


def entity_bucket(entity_type: str, entity_id: str) -> int:
    """The partition of an entity: the first 8 hex digits of SHA-1("type|id") mod BUCKETS.
    Must equal bucket_sql() in BigQuery (checked: test_build_summaries.py)."""
    import hashlib
    return int(hashlib.sha1(f"{entity_type}|{entity_id}".encode()).hexdigest()[:8], 16) % BUCKETS


def bucket_sql(type_expr: str, id_expr: str) -> str:
    return (f"MOD(CAST(CONCAT('0x', SUBSTR(TO_HEX(SHA1(CONCAT({type_expr}, '|', {id_expr}))), "
            f"1, 8)) AS INT64), {BUCKETS})")
