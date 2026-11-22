-- Title: MAUDE input profile for the matching data monitor
-- Check: what the MAUDE device rows of one window look like to the matcher: identifier coverage,
--   exact UDI links to GUDID, missing fields, product-code and manufacturer mix
-- Why: a shift in these is data drift for the MAUDE -> GUDID matcher (it learned from the rows
--   before the label snapshot); the UDI link rate is a decay signal that needs no hand labels
-- Informs: scripts/m01_data_drift.py (baseline and check), monitor/thresholds.json
WITH g AS (
  SELECT DISTINCT udi_di FROM `__GUDID__` WHERE udi_di IS NOT NULL AND udi_di != ''
),
m AS (
  SELECT udi_di, product_code, manufacturer_name_norm, model_number_raw, catalog_number
  FROM `__MAUDE__`
  WHERE {window}
),
j AS (
  SELECT m.*, g.udi_di IS NOT NULL AS in_gudid FROM m LEFT JOIN g USING (udi_di)
)
SELECT
  (SELECT AS STRUCT
     COUNT(*) AS n,
     COUNTIF(udi_di IS NOT NULL AND udi_di != '') AS n_udi,
     COUNTIF(in_gudid) AS n_udi_in_gudid,
     COUNTIF(model_number_raw IS NULL OR TRIM(model_number_raw) = '') AS n_no_model,
     COUNTIF(catalog_number IS NULL OR TRIM(catalog_number) = '') AS n_no_catalog,
     COUNTIF(manufacturer_name_norm IS NULL OR manufacturer_name_norm = '') AS n_no_manufacturer,
     COUNTIF(product_code IS NULL OR product_code = '') AS n_no_product_code
   FROM j) AS totals,
  ARRAY(SELECT AS STRUCT IFNULL(product_code, '') AS k, COUNT(*) AS n FROM j
        GROUP BY 1 ORDER BY n DESC, k LIMIT 300) AS product_codes,
  ARRAY(SELECT AS STRUCT IFNULL(manufacturer_name_norm, '') AS k, COUNT(*) AS n FROM j
        GROUP BY 1 ORDER BY n DESC, k LIMIT 300) AS manufacturers
