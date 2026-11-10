-- Title: Field fill rates (MAUDE by year, GUDID overall)
-- Check: Share of rows where each matching field is present (not NULL and not blank after TRIM):
--   UDI-DI, model number, catalog number, brand/device name, manufacturer, product code.
--   MAUDE is broken out by year of record_date; GUDID is one row (year = NULL).
-- Why: The definition can only lean on fields that are actually filled. Years with low UDI fill are where the
--   model must link without a UDI, and where UDI-derived labels say nothing.
-- Informs: Which fields the definition requires vs. treats as supporting evidence; size of the no-UDI pool.
SELECT
  'maude' AS source,
  EXTRACT(YEAR FROM record_date) AS year,
  COUNT(*) AS n_rows,
  ROUND(COUNTIF(NULLIF(TRIM(udi_di), '') IS NOT NULL) / COUNT(*), 4) AS pct_udi_di,
  ROUND(COUNTIF(NULLIF(TRIM(model_number_raw), '') IS NOT NULL) / COUNT(*), 4) AS pct_model,
  ROUND(COUNTIF(NULLIF(TRIM(catalog_number), '') IS NOT NULL) / COUNT(*), 4) AS pct_catalog,
  ROUND(COUNTIF(NULLIF(TRIM(model_number_raw), '') IS NOT NULL
             OR NULLIF(TRIM(catalog_number), '') IS NOT NULL) / COUNT(*), 4) AS pct_model_or_catalog,
  ROUND(COUNTIF(NULLIF(TRIM(device_name_raw), '') IS NOT NULL) / COUNT(*), 4) AS pct_device_name,
  ROUND(COUNTIF(NULLIF(TRIM(manufacturer_name_norm), '') IS NOT NULL) / COUNT(*), 4) AS pct_manufacturer,
  ROUND(COUNTIF(NULLIF(TRIM(product_code), '') IS NOT NULL) / COUNT(*), 4) AS pct_product_code
FROM `__MAUDE__`
GROUP BY year
UNION ALL
SELECT
  'gudid',
  NULL,
  COUNT(*),
  ROUND(COUNTIF(NULLIF(TRIM(udi_di), '') IS NOT NULL) / COUNT(*), 4),
  ROUND(COUNTIF(NULLIF(TRIM(model_number_raw), '') IS NOT NULL) / COUNT(*), 4),
  ROUND(COUNTIF(NULLIF(TRIM(catalog_number), '') IS NOT NULL) / COUNT(*), 4),
  ROUND(COUNTIF(NULLIF(TRIM(model_number_raw), '') IS NOT NULL
             OR NULLIF(TRIM(catalog_number), '') IS NOT NULL) / COUNT(*), 4),
  ROUND(COUNTIF(NULLIF(TRIM(device_name_raw), '') IS NOT NULL) / COUNT(*), 4),
  ROUND(COUNTIF(NULLIF(TRIM(manufacturer_name_norm), '') IS NOT NULL) / COUNT(*), 4),
  ROUND(COUNTIF(ARRAY_LENGTH(product_codes) > 0) / COUNT(*), 4)
FROM `__GUDID__`
ORDER BY source DESC, year
