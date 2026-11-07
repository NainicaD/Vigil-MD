"""Settings for the query layer. Each can be overridden by an environment variable.

The numbers marked PLACEHOLDER are starting values, to be set from measurements:
  - MAX_GB_PER_QUESTION: free dry runs of real questions (python -m query_layer.estimate);
    about 2-3 times the largest normal question.
  - NARRATIVES_PER_QUESTION / NARRATIVE_MAX_CHARS: a teammate check of variants
    (e.g. 3 x 300, 5 x 600, 8 x full text) on a few devices.
"""
from __future__ import annotations

import os

PROJECT = os.environ.get("VIGIL_BQ_PROJECT", "vigil-md-1792696816")
# vigil_md_prod in production; vigil_md_dev (the development extract) for development
DATASET = os.environ.get("VIGIL_BQ_DATASET", "vigil_md_prod")

MAX_GB_PER_QUESTION = float(os.environ.get("VIGIL_MAX_GB_PER_QUESTION", "2.0"))   # PLACEHOLDER

TOP_PROBLEMS = 10              # per problem type (device, patient)
MAX_RECALLS = 50               # newest first; the total count is always returned

NARRATIVES_PER_QUESTION = 5    # PLACEHOLDER
NARRATIVE_MAX_CHARS = 600      # PLACEHOLDER; cut at a word boundary, marked with "…"
NARRATIVE_MIN_CHARS = 50       # shorter texts ("SEE ATTACHED") are skipped
NARRATIVE_CANDIDATES = 20      # newest reports per event type considered for narratives
# Event types in the order narratives are picked: one of each that exists, then repeat
NARRATIVE_EVENT_ORDER = ("Death", "Injury", "Malfunction")

# The device key types the query layer accepts. The device lookup turns a user's words
# into one of these; the query layer never guesses.
KEY_TYPES = ("product_code", "brand", "manufacturer", "udi_di")
