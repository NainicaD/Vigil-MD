"""Settings for the query layer. Each can be overridden by an environment variable.

The query layer reads the daily summary tables (Data-Pipeline/scripts/warehouse/
build_summaries.py), so a question costs MBs, not GBs. Measured on vigil_md_prod
(2026-11-07): one lookup reads one partition, e.g. DreamStation's report links 3.1 MB; the
largest product code (DZE) about 114 MB.

Marked PLACEHOLDER: to be set from a teammate check of variants (e.g. 3 x 300, 5 x 600, 8 x
full text) on a few devices.
"""
from __future__ import annotations

import hashlib
import os

PROJECT = os.environ.get("VIGIL_BQ_PROJECT", "vigil-md-1792696816")
# vigil_md_prod in production; vigil_md_dev (the development extract) for development
DATASET = os.environ.get("VIGIL_BQ_DATASET", "vigil_md_prod")

# A normal question bills about 40 MB (BigQuery's 10 MB minimum per table read), the largest
# product codes about 200 MB with narratives. The cap stops anything far beyond that.
MAX_GB_PER_QUESTION = float(os.environ.get("VIGIL_MAX_GB_PER_QUESTION", "0.5"))

TOP_PROBLEMS = 10              # per problem type (device, patient); the tables keep 25
MAX_RECALLS = 50               # newest first; the total count is always returned

NARRATIVES_PER_QUESTION = 5    # PLACEHOLDER
NARRATIVE_MAX_CHARS = 600      # PLACEHOLDER; cut at a word boundary, marked with "…"
NARRATIVE_MIN_CHARS = 50       # shorter texts ("SEE ATTACHED") are skipped
NARRATIVE_CANDIDATES = 20      # newest reports per event type considered for narratives
# Event types in the order narratives are picked: one of each that exists, then repeat
NARRATIVE_EVENT_ORDER = ("Death", "Injury", "Malfunction")

# What the query layer is asked about: the same entities as the summary tables and the
# search index. device = GUDID records grouped by company and brand (entity_id "dev:…");
# product_code = an FDA product code; manufacturer = manufacturer_name_norm.
ENTITY_TYPES = ("device", "product_code", "manufacturer")

# The summary tables are partitioned by this bucket. It must equal the pipeline's
# Data-Pipeline/scripts/warehouse/summary_config.py entity_bucket() (both tests check the same
# values computed by BigQuery).
BUCKETS = 1000


def entity_bucket(entity_type: str, entity_id: str) -> int:
    return int(hashlib.sha1(f"{entity_type}|{entity_id}".encode()).hexdigest()[:8], 16) % BUCKETS
