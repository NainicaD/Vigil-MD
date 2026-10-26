-- Staging: recall. Port of flatten_recall (flatten_to_parquet.py:99-121).
-- build_staging.py fills in the raw table name, defines norm_date / norm_mfr,
-- and adds record_hash, the null-key filter and the dedup.
SELECT
  'recall'                                                   AS source,
  NULLIF(JSON_VALUE(payload, '$.product_res_number'), '')    AS source_record_id,
  NULLIF(JSON_VALUE(payload, '$.product_res_number'), '')    AS link_key,
  NULLIF(JSON_VALUE(payload, '$.res_event_number'), '')      AS res_event_number,
  NULLIF(JSON_VALUE(payload, '$.recalling_firm'), '')        AS manufacturer_name_raw,
  norm_mfr(JSON_VALUE(payload, '$.recalling_firm'))          AS manufacturer_name_norm,
  NULLIF(JSON_VALUE(payload, '$.product_description'), '')   AS device_name_raw,
  NULLIF(JSON_VALUE(payload, '$.product_code'), '')          AS product_code,
  NULLIF(JSON_VALUE(payload, '$.reason_for_recall'), '')     AS reason_text,
  NULLIF(JSON_VALUE(payload, '$.code_info'), '')             AS scope_text,
  norm_date(JSON_VALUE(payload, '$.event_date_posted'))      AS record_date,
  NULLIF(JSON_VALUE(payload, '$.recall_status'), '')         AS recall_status,
  NULLIF(JSON_VALUE(payload, '$.root_cause_description'), '') AS root_cause,
  NULLIF(JSON_VALUE(payload, '$.action'), '')                AS action,
  _ingest_date,
  _loaded_at
FROM `__RAW_TABLE__`
WHERE _ingest_date = @ingest_date
