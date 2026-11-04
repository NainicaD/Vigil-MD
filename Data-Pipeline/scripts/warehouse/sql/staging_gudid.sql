-- Staging: GUDID. Columns of flatten_gudid (flatten_to_parquet.py:158-191),
-- mapped to the bulk XML release. The payload is one <device> converted by
-- scripts/warehouse/convert.py (fields at the top level, list wrappers as
-- arrays). XML text is kept exactly in raw, so text fields are trimmed here.
-- build_staging.py fills in the raw table name, defines norm_date / norm_mfr,
-- and adds record_hash, the null-key filter and the dedup.
SELECT
  'gudid'                                                          AS source,
  NULLIF(TRIM(JSON_VALUE(payload, '$.publicDeviceRecordKey')), '') AS source_record_id,
  -- Primary DI: identifiers.identifier[deviceIdType = "Primary"].deviceId
  (SELECT NULLIF(TRIM(JSON_VALUE(i, '$.deviceId')), '')
   FROM UNNEST(JSON_QUERY_ARRAY(payload, '$.identifiers.identifier')) AS i
   WHERE TRIM(JSON_VALUE(i, '$.deviceIdType')) = 'Primary'
   LIMIT 1)                                                        AS udi_di,
  NULLIF(TRIM(JSON_VALUE(payload, '$.companyName')), '')           AS manufacturer_name_raw,
  norm_mfr(JSON_VALUE(payload, '$.companyName'))                   AS manufacturer_name_norm,
  NULLIF(TRIM(JSON_VALUE(payload, '$.brandName')), '')             AS device_name_raw,
  NULLIF(TRIM(JSON_VALUE(payload, '$.versionModelNumber')), '')    AS model_number_raw,
  NULLIF(TRIM(JSON_VALUE(payload, '$.catalogNumber')), '')         AS catalog_number,
  -- productCodes.fdaProductCode[].productCode, in document order
  ARRAY(
    SELECT TRIM(JSON_VALUE(pc, '$.productCode'))
    FROM UNNEST(JSON_QUERY_ARRAY(payload, '$.productCodes.fdaProductCode')) AS pc WITH OFFSET AS o
    WHERE NULLIF(TRIM(JSON_VALUE(pc, '$.productCode')), '') IS NOT NULL
    ORDER BY o)                                                    AS product_codes,
  NULLIF(TRIM(JSON_VALUE(payload, '$.deviceRecordStatus')), '')    AS device_record_status,
  norm_date(JSON_VALUE(payload, '$.devicePublishDate'))            AS publish_date,
  NULLIF(TRIM(JSON_VALUE(payload, '$.deviceCommDistributionStatus')), '') AS distribution_status,
  _ingest_date,
  _loaded_at
FROM `__RAW_TABLE__`
WHERE _ingest_date = @ingest_date
