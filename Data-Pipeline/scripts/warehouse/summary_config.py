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
