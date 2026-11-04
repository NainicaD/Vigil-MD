-- Staging: MAUDE. Port of flatten_maude (flatten_to_parquet.py:124-155).
-- One row per device in device[]. A report with no devices keeps one row
-- (flatten_maude line 131: devices = [{}]), with device_seq 0.
-- build_staging.py fills in the raw table name, defines norm_date / norm_mfr,
-- and adds record_hash, the null-key filter and the dedup.
SELECT
  'maude'                                                    AS source,
  NULLIF(JSON_VALUE(r.payload, '$.mdr_report_key'), '')      AS source_record_id,
  device_seq,
  -- dev.get("manufacturer_d_name") or r.get("manufacturer_name")
  COALESCE(NULLIF(JSON_VALUE(dev, '$.manufacturer_d_name'), ''),
           NULLIF(JSON_VALUE(r.payload, '$.manufacturer_name'), ''))
                                                             AS manufacturer_name_raw,
  norm_mfr(COALESCE(NULLIF(JSON_VALUE(dev, '$.manufacturer_d_name'), ''),
                    JSON_VALUE(r.payload, '$.manufacturer_name')))
                                                             AS manufacturer_name_norm,
  NULLIF(JSON_VALUE(dev, '$.brand_name'), '')                AS device_name_raw,
  NULLIF(JSON_VALUE(dev, '$.model_number'), '')              AS model_number_raw,
  NULLIF(JSON_VALUE(dev, '$.catalog_number'), '')            AS catalog_number,
  NULLIF(JSON_VALUE(dev, '$.lot_number'), '')                AS lot_number,
  NULLIF(JSON_VALUE(dev, '$.udi_di'), '')                    AS udi_di,
  NULLIF(JSON_VALUE(dev, '$.device_report_product_code'), '') AS product_code,
  norm_date(JSON_VALUE(r.payload, '$.date_received'))        AS record_date,
  NULLIF(JSON_VALUE(r.payload, '$.event_type'), '')          AS event_type,
  NULLIF(JSON_VALUE(r.payload, '$.adverse_event_flag'), '')  AS adverse_event_flag,
  NULLIF(JSON_VALUE(r.payload, '$.report_number'), '')       AS report_number,
  r._ingest_date,
  r._loaded_at
FROM `__RAW_TABLE__` AS r
CROSS JOIN UNNEST(
  IF(ARRAY_LENGTH(IFNULL(JSON_QUERY_ARRAY(r.payload, '$.device'), [])) = 0,
     [JSON '{}'],
     JSON_QUERY_ARRAY(r.payload, '$.device'))
) AS dev WITH OFFSET AS device_seq
WHERE r._ingest_date = @ingest_date
