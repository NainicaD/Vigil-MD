-- Title: Label set counts and fill rates per split
-- Check: Part `counts`: rows by split x label x label_source x match kind. Part `years`: rows by split x
--   MAUDE year (the weights the C4 fill-rate baseline needs). Part `fill`: share of rows with each field
--   present, per split and side, with the same definition as sql/same_device/02_fill_rates.sql (not NULL
--   and not blank after TRIM; GUDID product code = non-empty array) so the two are comparable.
-- Why: validate_labels.py --bq reads these small tables instead of scanning the full label set. The label
--   set is drawn from UDI-linked, identifier-bearing rows, so fill rates should come out ABOVE the
--   baseline; a drop below means a broken column mapping or join.
-- Informs: validate_labels.py (C4, C8) and LABELS.md.
-- Placeholders filled by build_labels.py: __MODEL_DATASET__, __LABEL_SET__ (label_set_v1, label_set_v2_e1, ...).
-- @part counts
CREATE OR REPLACE TABLE `__MODEL_DATASET__.label_stats_counts` AS
SELECT meta_split, label, label_source, key_match_kind, COUNT(*) AS n_rows
FROM `__MODEL_DATASET__.__LABEL_SET__`
GROUP BY meta_split, label, label_source, key_match_kind
-- @part years
CREATE OR REPLACE TABLE `__MODEL_DATASET__.label_stats_years` AS
SELECT meta_split, meta_year, COUNT(*) AS n_rows
FROM `__MODEL_DATASET__.__LABEL_SET__`
GROUP BY meta_split, meta_year
-- @part fill
CREATE OR REPLACE TABLE `__MODEL_DATASET__.label_stats_fill` AS
SELECT meta_split, 'maude' AS side, COUNT(*) AS n_rows,
       ROUND(COUNTIF(NULLIF(TRIM(key_m_model_raw), '') IS NOT NULL) / COUNT(*), 4) AS pct_model,
       ROUND(COUNTIF(NULLIF(TRIM(key_m_catalog_raw), '') IS NOT NULL) / COUNT(*), 4) AS pct_catalog,
       ROUND(COUNTIF(NULLIF(TRIM(key_m_model_raw), '') IS NOT NULL
                  OR NULLIF(TRIM(key_m_catalog_raw), '') IS NOT NULL) / COUNT(*), 4) AS pct_model_or_catalog,
       ROUND(COUNTIF(NULLIF(TRIM(key_m_name), '') IS NOT NULL) / COUNT(*), 4) AS pct_device_name,
       ROUND(COUNTIF(NULLIF(TRIM(key_m_mfr), '') IS NOT NULL) / COUNT(*), 4) AS pct_manufacturer,
       ROUND(COUNTIF(NULLIF(TRIM(key_m_pc), '') IS NOT NULL) / COUNT(*), 4) AS pct_product_code
FROM `__MODEL_DATASET__.__LABEL_SET__`
GROUP BY meta_split
UNION ALL
SELECT meta_split, 'gudid', COUNT(*),
       ROUND(COUNTIF(NULLIF(TRIM(key_g_model_raw), '') IS NOT NULL) / COUNT(*), 4),
       ROUND(COUNTIF(NULLIF(TRIM(key_g_catalog_raw), '') IS NOT NULL) / COUNT(*), 4),
       ROUND(COUNTIF(NULLIF(TRIM(key_g_model_raw), '') IS NOT NULL
                  OR NULLIF(TRIM(key_g_catalog_raw), '') IS NOT NULL) / COUNT(*), 4),
       ROUND(COUNTIF(NULLIF(TRIM(key_g_name), '') IS NOT NULL) / COUNT(*), 4),
       ROUND(COUNTIF(NULLIF(TRIM(key_g_mfr), '') IS NOT NULL) / COUNT(*), 4),
       ROUND(COUNTIF(ARRAY_LENGTH(key_g_pcs) > 0) / COUNT(*), 4)
FROM `__MODEL_DATASET__.__LABEL_SET__`
GROUP BY meta_split
