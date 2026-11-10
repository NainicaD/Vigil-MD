-- Title: The label set: union, dedup, pair_id
-- Check: Stacks positives and the three negative sources, keeps one row per (maude labeler, model key,
--   catalog key, product code, udi_di) with positives winning ties, assigns pair_id, and fails the
--   statement if any row has no split.
-- Why: One table is what the validator and training read. Column namespaces follow
--   scripts/same_device_features.py: key_* join and audit columns (raw fields the Python feature code
--   builds feat_* from, plus key_gudid_udi_di), label / label_source, meta_* split, device key, year,
--   duplicate count. No feat_* column is written here: features are computed in Python from the key_*
--   fields, so the UDI never has a path into one.
-- Informs: validate_labels.py (C1 pair_id, C2 and C6 split, C7 dedup) and 06_label_stats.sql.
-- Placeholders filled by build_labels.py: __MODEL_DATASET__.
CREATE OR REPLACE TABLE `__MODEL_DATASET__.label_set_v1` AS
WITH u AS (
  SELECT 'MATCH' AS label, 'udi_link' AS label_source, 1 AS prio,
         mdr_report_key, device_seq, udi_di, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, g_mfr, g_pcs, g_name, g_model_raw, g_catalog_raw, g_model, g_cat, n_dis_for_model,
         meta_device_key, meta_split, match_kind
  FROM `__MODEL_DATASET__.positives`
  UNION ALL
  SELECT 'NON_MATCH', 'suffix_sibling', 2,
         mdr_report_key, device_seq, udi_di, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, g_mfr, g_pcs, g_name, g_model_raw, g_catalog_raw, g_model, g_cat, n_dis_for_model,
         meta_device_key, meta_split, match_kind
  FROM `__MODEL_DATASET__.negatives_suffix`
  UNION ALL
  SELECT 'NON_MATCH', 'same_labeler_hard', 3,
         mdr_report_key, device_seq, udi_di, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, g_mfr, g_pcs, g_name, g_model_raw, g_catalog_raw, g_model, g_cat, n_dis_for_model,
         meta_device_key, meta_split, match_kind
  FROM `__MODEL_DATASET__.negatives_same_labeler`
  UNION ALL
  SELECT 'NON_MATCH', 'random_labeler', 4,
         mdr_report_key, device_seq, udi_di, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, g_mfr, g_pcs, g_name, g_model_raw, g_catalog_raw, g_model, g_cat, n_dis_for_model,
         meta_device_key, meta_split, match_kind
  FROM `__MODEL_DATASET__.negatives_easy`
),
deduped AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT *, ROW_NUMBER() OVER (
             PARTITION BY m_mfr, m_model, m_cat, m_pc, udi_di
             ORDER BY prio, FARM_FINGERPRINT(CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), '')))
           ) AS rn
    FROM u
  )
  WHERE rn = 1
)
SELECT
  {{ pair_id(mdr_report_key, device_seq, udi_di, label_source) }} AS pair_id,
  mdr_report_key AS key_mdr_report_key, device_seq AS key_device_seq, udi_di AS key_gudid_udi_di,
  m_mfr AS key_m_mfr, m_pc AS key_m_pc, m_name AS key_m_name, m_model_raw AS key_m_model_raw,
  m_catalog_raw AS key_m_catalog_raw, m_model AS key_m_model, m_cat AS key_m_cat,
  g_mfr AS key_g_mfr, g_pcs AS key_g_pcs, g_name AS key_g_name, g_model_raw AS key_g_model_raw,
  g_catalog_raw AS key_g_catalog_raw, g_model AS key_g_model, g_cat AS key_g_cat,
  match_kind AS key_match_kind,
  label, label_source,
  meta_device_key, meta_split, yr AS meta_year, n_dup_rows AS meta_n_dup_rows,
  n_dis_for_model AS meta_n_dis_for_model
FROM deduped
WHERE IF((SELECT COUNTIF(meta_split IS NULL) FROM deduped) > 0,
         ERROR('label set has rows without a split: device_split is missing a base_key'), TRUE)
