"""Which MAUDE narrative texts go into vigil_md_prod.maude_text.

Defaults for scripts/warehouse/build_maude_events.py --step text. Each value
can be overridden on the command line (--text-types, --event-types,
--from-year, --to-year).

The staging table (maude_events_stg) keeps every text of an ingest date for
14 days, so a changed filter can be applied again from staging within that
time without scanning RAW. maude_problems is not filtered.

text_type_code values seen in the openFDA files (2016–2026, checked 2026-11-07):
  "Description of Event or Problem"     what happened (the reporter's account)
  "Additional Manufacturer Narrative"   the manufacturer's follow-up
openFDA has no device-evaluation text type.

event_types: Malfunction added 2026-11-07 (team decision), so the chatbot can
describe malfunctions without harm too. On the 2026-11-03 export this keeps
24,397,943 texts from 20,897,284 reports (8.6 GB of text); Death and Injury
alone were 9,227,647 texts. Other, "No answer provided" and reports without
an event type (about 10,800 reports) stay out.
"""

TEXT_FILTER = {
    "text_types":  ["Description of Event or Problem"],
    "event_types": ["Death", "Injury", "Malfunction"],
    "from_year":   2016,      # on date_received, inclusive
    "to_year":     2026,      # inclusive
}
