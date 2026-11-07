-- Staging: MAUDE event fields, one row per report (mdr_report_key).
-- Fields that staging_maude.sql does not keep: device and patient problem terms,
-- event dates, and the narrative text array (kept as JSON; the text step reads it).
-- build_maude_events.py fills in the raw table name, defines norm_date, and adds
-- record_hash, the null-key filter and the dedup.
SELECT
  NULLIF(JSON_VALUE(r.payload, '$.mdr_report_key'), '')      AS mdr_report_key,
  NULLIF(JSON_VALUE(r.payload, '$.event_type'), '')          AS event_type,
  norm_date(JSON_VALUE(r.payload, '$.date_received'))        AS date_received,
  norm_date(JSON_VALUE(r.payload, '$.date_of_event'))        AS date_of_event,
  norm_date(JSON_VALUE(r.payload, '$.date_report'))          AS date_report,
  -- product_problems: ["Break", "False Alarm", ...]
  ARRAY(
    SELECT DISTINCT TRIM(JSON_VALUE(p))
    FROM UNNEST(JSON_QUERY_ARRAY(r.payload, '$.product_problems')) AS p
    WHERE NULLIF(TRIM(JSON_VALUE(p)), '') IS NOT NULL
    ORDER BY 1
  )                                                          AS product_problems,
  -- patient[].patient_problems: one list per patient, often repeated
  ARRAY(
    SELECT DISTINCT TRIM(JSON_VALUE(pp))
    FROM UNNEST(JSON_QUERY_ARRAY(r.payload, '$.patient')) AS pat,
         UNNEST(JSON_QUERY_ARRAY(pat, '$.patient_problems')) AS pp
    WHERE NULLIF(TRIM(JSON_VALUE(pp)), '') IS NOT NULL
    ORDER BY 1
  )                                                          AS patient_problems,
  -- mdr_text: [{mdr_text_key, text_type_code, patient_sequence_number, text}, ...]
  JSON_QUERY(r.payload, '$.mdr_text')                        AS mdr_text,
  r._ingest_date,
  r._loaded_at
FROM `__RAW_TABLE__` AS r
WHERE r._ingest_date = @ingest_date
