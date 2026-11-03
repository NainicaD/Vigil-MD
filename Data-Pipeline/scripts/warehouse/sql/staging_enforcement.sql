-- Staging: enforcement. Port of flatten_enforcement (flatten_to_parquet.py:73-96).
-- build_staging.py fills in the raw table name, defines norm_date / norm_mfr,
-- and adds record_hash, the null-key filter and the dedup.
SELECT
  'enforcement'                                              AS source,
  NULLIF(JSON_VALUE(payload, '$.recall_number'), '')         AS source_record_id,
  NULLIF(JSON_VALUE(payload, '$.recall_number'), '')         AS link_key,
  NULLIF(JSON_VALUE(payload, '$.event_id'), '')              AS event_id,
  NULLIF(JSON_VALUE(payload, '$.recalling_firm'), '')        AS manufacturer_name_raw,
  norm_mfr(JSON_VALUE(payload, '$.recalling_firm'))          AS manufacturer_name_norm,
  NULLIF(JSON_VALUE(payload, '$.product_description'), '')   AS device_name_raw,
  NULLIF(JSON_VALUE(payload, '$.classification'), '')        AS recall_class,
  NULLIF(JSON_VALUE(payload, '$.reason_for_recall'), '')     AS reason_text,
  -- " ".join(filter(None, [code_info, more_code_info])) or None
  NULLIF(ARRAY_TO_STRING([
      NULLIF(JSON_VALUE(payload, '$.code_info'), ''),
      NULLIF(JSON_VALUE(payload, '$.more_code_info'), '')], ' '), '')
                                                             AS scope_text,
  norm_date(JSON_VALUE(payload, '$.report_date'))            AS record_date,
  NULLIF(JSON_VALUE(payload, '$.status'), '')                AS status,
  NULLIF(JSON_VALUE(payload, '$.distribution_pattern'), '')  AS distribution_pattern,
  _ingest_date,
  _loaded_at
FROM `__RAW_TABLE__`
WHERE _ingest_date = @ingest_date
