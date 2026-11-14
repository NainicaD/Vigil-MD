-- Title: Do UDI-linked MAUDE and GUDID records agree on model, manufacturer and product code?
-- Check: Joins MAUDE rows to GUDID on exact primary DI (GUDID DIs are unique, query 04). For each
--   linked row, classifies how the identifiers relate, first match wins: same model key; MAUDE
--   model = GUDID catalog; MAUDE catalog = GUDID model; catalog = catalog; MAUDE model missing or
--   placeholder; one model key is a prefix of the other; one contains the other; otherwise a
--   conflict. Part `crosstab` crosses that with manufacturer and product-code agreement. Part
--   `conflict_examples` lists 25 linked rows that land in `conflict`.
-- Why: The training labels are "UDI matches = positive". The `conflict` share is the best estimate
--   of label noise (or of MAUDE UDI entry errors). The cross-field categories show which
--   identifier comparisons the definition must allow so true matches are not called NON_MATCH.
-- Informs: Edge cases "same primary DI", "model/catalog swap" and "prefix"; which UDI-linked rows
--   are kept as training positives.
-- @part crosstab
WITH g AS (
  SELECT udi_di, manufacturer_name_norm AS g_mfr, product_codes AS g_pcs,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS g_model,
         UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS g_cat
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
m AS (
  SELECT udi_di, manufacturer_name_norm AS m_mfr, product_code AS m_pc,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS m_model,
         UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS m_cat
  FROM `__MAUDE__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
linked AS (
  SELECT m.*, g.g_mfr, g.g_pcs, g.g_model, g.g_cat,
    (m.m_model IS NULL OR LENGTH(m.m_model) < 2 OR REGEXP_CONTAINS(m.m_model,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')) AS m_missing,
    (m.m_cat IS NOT NULL AND LENGTH(m.m_cat) >= 2 AND NOT REGEXP_CONTAINS(m.m_cat,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')) AS m_cat_ok
  FROM m JOIN g USING (udi_di)
)
SELECT
  CASE
    WHEN NOT m_missing AND m_model = g_model THEN 'same_model_key'
    WHEN NOT m_missing AND m_model = g_cat THEN 'maude_model_eq_gudid_catalog'
    WHEN m_cat_ok AND m_cat = g_model THEN 'maude_catalog_eq_gudid_model'
    WHEN m_cat_ok AND m_cat = g_cat THEN 'catalog_eq_catalog'
    WHEN m_missing THEN 'maude_model_missing_or_placeholder'
    WHEN g_model IS NULL OR g_model = '' THEN 'gudid_model_missing'
    WHEN STARTS_WITH(g_model, m_model) OR STARTS_WITH(m_model, g_model) THEN 'prefix_related'
    WHEN LENGTH(m_model) >= 3 AND LENGTH(g_model) >= 3
         AND (STRPOS(m_model, g_model) > 0 OR STRPOS(g_model, m_model) > 0) THEN 'contains'
    ELSE 'conflict'
  END AS model_status,
  CASE WHEN m_mfr IS NULL OR g_mfr IS NULL THEN 'missing'
       WHEN m_mfr = g_mfr THEN 'agree' ELSE 'differ' END AS mfr_status,
  CASE WHEN m_pc IS NULL OR ARRAY_LENGTH(g_pcs) = 0 THEN 'missing'
       WHEN m_pc IN UNNEST(g_pcs) THEN 'agree' ELSE 'differ' END AS product_code_status,
  COUNT(*) AS n_rows,
  ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 6) AS share_of_linked
FROM linked
GROUP BY model_status, mfr_status, product_code_status
ORDER BY n_rows DESC
-- @part conflict_examples
WITH g AS (
  SELECT udi_di, manufacturer_name_norm AS g_mfr, model_number_raw AS g_model_raw,
         catalog_number AS g_catalog, device_name_raw AS g_device_name,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS g_model,
         UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS g_cat
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
m AS (
  SELECT source_record_id AS mdr_report_key, device_seq, udi_di,
         manufacturer_name_norm AS m_mfr, model_number_raw AS m_model_raw,
         catalog_number AS m_catalog, device_name_raw AS m_device_name,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS m_model,
         UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS m_cat
  FROM `__MAUDE__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
)
SELECT m.mdr_report_key, m.device_seq, udi_di,
       m.m_mfr, g.g_mfr, m.m_model_raw, g.g_model_raw, m.m_catalog, g.g_catalog,
       m.m_device_name, g.g_device_name
FROM m JOIN g USING (udi_di)
WHERE LENGTH(m.m_model) >= 2
  AND NOT REGEXP_CONTAINS(m.m_model,
    r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
  AND g.g_model IS NOT NULL AND g.g_model != ''
  AND m.m_model != g.g_model
  AND m.m_model != IFNULL(g.g_cat, '')
  AND IFNULL(m.m_cat, '') != g.g_model
  AND (m.m_cat IS NULL OR LENGTH(m.m_cat) < 2 OR m.m_cat != IFNULL(g.g_cat, ''))
  AND NOT STARTS_WITH(g.g_model, m.m_model) AND NOT STARTS_WITH(m.m_model, g.g_model)
  AND NOT (LENGTH(m.m_model) >= 3 AND LENGTH(g.g_model) >= 3
           AND (STRPOS(m.m_model, g.g_model) > 0 OR STRPOS(g.g_model, m.m_model) > 0))
ORDER BY FARM_FINGERPRINT(CONCAT(m.mdr_report_key, '|', CAST(m.device_seq AS STRING)))
LIMIT 25
