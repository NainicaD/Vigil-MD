-- Title: Inventory of the two tables being linked
-- Check: Row counts, distinct records, distinct UDI-DIs, manufacturers and model numbers, and
--   the date range, for prod MAUDE (one row per device in a report) and prod GUDID.
-- Why: Sanity check that we read the right tables (expect about 21.5M MAUDE and 5.24M GUDID rows,
--   as in the 2026-10-26 bias snapshot) and gives the scale every later share is measured against.
-- Informs: Scale only; no same-device rule depends on it directly.
SELECT
  'maude' AS source,
  COUNT(*) AS n_rows,
  COUNT(DISTINCT source_record_id) AS n_records,
  COUNT(DISTINCT udi_di) AS n_distinct_udi_di,
  COUNT(DISTINCT manufacturer_name_norm) AS n_manufacturers_norm,
  COUNT(DISTINCT UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', ''))) AS n_model_keys,
  MIN(record_date) AS min_date,
  MAX(record_date) AS max_date
FROM `__MAUDE__`
UNION ALL
SELECT
  'gudid',
  COUNT(*),
  COUNT(DISTINCT source_record_id),
  COUNT(DISTINCT udi_di),
  COUNT(DISTINCT manufacturer_name_norm),
  COUNT(DISTINCT UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', ''))),
  MIN(publish_date),
  MAX(publish_date)
FROM `__GUDID__`
