-- Title: Is one GUDID primary DI always one device?
-- Check: Groups GUDID by udi_di. Counts DIs that appear in more than one GUDID record
--   (publicDeviceRecordKey), and of those, how many disagree on manufacturer or on model key
--   (model number upper-cased with spaces and - _ . / removed). Part `examples` lists the 25 DIs
--   with the most disagreement.
-- Why: The UDI-derived training labels assume "same primary DI = same device". If one DI maps to
--   records with different models, that assumption (and the labels) is wrong for those DIs.
-- Informs: Edge case "same primary DI": is it always MATCH, or MATCH only when the model agrees?
-- @part summary
WITH g AS (
  SELECT udi_di, source_record_id, manufacturer_name_norm,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
per_di AS (
  SELECT udi_di,
         COUNT(*) AS n_records,
         COUNT(DISTINCT manufacturer_name_norm) AS n_mfr,
         COUNT(DISTINCT model_key) AS n_model_keys
  FROM g GROUP BY udi_di
)
SELECT
  COUNT(*) AS n_distinct_di,
  COUNTIF(n_records > 1) AS n_di_multi_record,
  COUNTIF(n_records > 1 AND n_model_keys > 1) AS n_di_multi_model,
  COUNTIF(n_records > 1 AND n_mfr > 1) AS n_di_multi_mfr,
  ROUND(COUNTIF(n_records > 1) / COUNT(*), 6) AS pct_di_multi_record,
  ROUND(COUNTIF(n_model_keys > 1) / COUNT(*), 6) AS pct_di_multi_model,
  MAX(n_records) AS max_records_per_di
FROM per_di
-- @part examples
WITH g AS (
  SELECT udi_di, source_record_id, manufacturer_name_norm, model_number_raw, catalog_number,
         device_name_raw, device_record_status, publish_date,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
)
SELECT
  udi_di,
  COUNT(*) AS n_records,
  COUNT(DISTINCT manufacturer_name_norm) AS n_mfr,
  COUNT(DISTINCT model_key) AS n_model_keys,
  STRING_AGG(DISTINCT manufacturer_name_norm, ' | ' LIMIT 5) AS manufacturers,
  STRING_AGG(DISTINCT model_number_raw, ' | ' LIMIT 5) AS models,
  STRING_AGG(DISTINCT device_name_raw, ' | ' LIMIT 5) AS device_names,
  STRING_AGG(DISTINCT device_record_status, ' | ' LIMIT 5) AS statuses
FROM g
GROUP BY udi_di
HAVING COUNT(*) > 1
ORDER BY n_model_keys DESC, n_mfr DESC, n_records DESC, udi_di
LIMIT 25
