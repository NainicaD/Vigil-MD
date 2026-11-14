-- Title: Model number and catalog number used interchangeably
-- Check: On MAUDE rows linked to GUDID by exact primary DI where both sides have a usable model
--   key, counts which identifier fields agree: model = model, MAUDE model = GUDID catalog,
--   MAUDE catalog = GUDID model, catalog = catalog. Each row of the result is one combination.
-- Why: Reporters often type the catalog number into the model field (or the reverse). If the definition only
--   accepts model = model, these true matches become false negatives. If catalog numbers are
--   shared across different devices, accepting them would cause false merges.
-- Informs: Edge case "model/catalog swap": does a cross-field match count as MATCH?
WITH g AS (
  SELECT udi_di,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS g_model,
         UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS g_cat
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY udi_di ORDER BY publish_date DESC, source_record_id) = 1
),
m AS (
  SELECT udi_di,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS m_model,
         UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS m_cat
  FROM `__MAUDE__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
linked AS (
  SELECT
    NULLIF(m_model, '') AS m_model, NULLIF(m_cat, '') AS m_cat,
    NULLIF(g_model, '') AS g_model, NULLIF(g_cat, '') AS g_cat
  FROM m JOIN g USING (udi_di)
)
SELECT
  IFNULL(m_model = g_model, FALSE) AS model_eq_model,
  IFNULL(m_model = g_cat, FALSE) AS maude_model_eq_gudid_catalog,
  IFNULL(m_cat = g_model, FALSE) AS maude_catalog_eq_gudid_model,
  IFNULL(m_cat = g_cat, FALSE) AS catalog_eq_catalog,
  IFNULL(g_model = g_cat, FALSE) AS gudid_model_eq_own_catalog,
  COUNT(*) AS n_rows,
  ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 6) AS share_of_linked
FROM linked
WHERE m_model IS NOT NULL OR m_cat IS NOT NULL
GROUP BY 1, 2, 3, 4, 5
ORDER BY n_rows DESC
