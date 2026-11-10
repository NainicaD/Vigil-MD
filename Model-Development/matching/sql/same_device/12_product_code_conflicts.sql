-- Title: Product-code agreement on UDI-linked rows
-- Check: On MAUDE rows linked to GUDID by exact primary DI, whether the MAUDE product code is one
--   of the GUDID record's product codes. Part `summary` gives the counts; part `top_conflicts`
--   lists the 40 most frequent (MAUDE code, GUDID codes) pairs that disagree.
-- Why: Product code (FDA device type) is a cheap guard against false merges between unrelated
--   devices with the same model string. If it disagrees often even on true UDI links (for example
--   related codes within one device family), it can only be a soft feature, not a hard rule.
-- Informs: Edge case "product-code conflict": NON_MATCH, or a warning only.
-- @part summary
WITH g AS (
  SELECT udi_di, product_codes
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY udi_di ORDER BY publish_date DESC, source_record_id) = 1
)
SELECT
  CASE WHEN NULLIF(TRIM(m.product_code), '') IS NULL THEN 'maude_missing'
       WHEN ARRAY_LENGTH(g.product_codes) = 0 OR g.product_codes IS NULL THEN 'gudid_missing'
       WHEN m.product_code IN UNNEST(g.product_codes) THEN 'agree'
       ELSE 'differ' END AS product_code_status,
  COUNT(*) AS n_rows,
  ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 6) AS share_of_linked
FROM `__MAUDE__` AS m
JOIN g USING (udi_di)
GROUP BY product_code_status
ORDER BY n_rows DESC
-- @part top_conflicts
WITH g AS (
  SELECT udi_di, product_codes
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY udi_di ORDER BY publish_date DESC, source_record_id) = 1
)
SELECT
  m.product_code AS maude_product_code,
  ARRAY_TO_STRING(g.product_codes, ',') AS gudid_product_codes,
  COUNT(*) AS n_rows,
  COUNT(DISTINCT udi_di) AS n_dis
FROM `__MAUDE__` AS m
JOIN g USING (udi_di)
WHERE NULLIF(TRIM(m.product_code), '') IS NOT NULL
  AND ARRAY_LENGTH(g.product_codes) > 0
  AND m.product_code NOT IN UNNEST(g.product_codes)
GROUP BY maude_product_code, gudid_product_codes
ORDER BY n_rows DESC, maude_product_code
LIMIT 40
