-- Title: Training positives from UDI links (H2)
-- Check: Part `gudid_keyed` keys every GUDID record (identifier keys, device key, split, DIs per model
--   key). Part `linked_all` joins MAUDE rows that carry a udi_di to GUDID on primary DI and collapses
--   them to one row per (maude labeler, model key, catalog key, product code, udi_di), with the
--   duplicate count. Part `review_conflicts` keeps the match_kind = 'conflict' rows (E1) for review. Part
--   `positives` drops conflicts and model-key-only agreements on a non-distinctive key (E4), then caps
--   rows per device.
-- Why: UDI links give free positives, but 21.5M MAUDE rows carry 194K DIs. Without the dedup, Alaris 8100
--   (994K rows) and Tandem 1002717 (704K rows) dominate every metric. Conflicts are label noise, not
--   negatives: many are a component report that carries the system's UDI.
-- Informs: sql/labels/03 to 05. Needs vigil_md_model.device_split (columns base_key, device_key, split).
-- Placeholders filled by build_labels.py: __MODEL_DATASET__, __MAX_PER_DEVICE__. No regex literals here:
--   rules come from scripts/same_device_features.py as macros.
-- @part gudid_keyed
CREATE OR REPLACE TABLE `__MODEL_DATASET__.gudid_keyed` AS
WITH g AS (
  SELECT udi_di, manufacturer_name_norm AS g_mfr, product_codes AS g_pcs, device_name_raw AS g_name,
         model_number_raw AS g_model_raw, catalog_number AS g_catalog_raw,
         {{ identifier_key(model_number_raw) }} AS g_model,
         {{ identifier_key(catalog_number) }} AS g_cat,
         {{ device_key(manufacturer_name_norm, model_number_raw, catalog_number) }} AS base_key
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
)
SELECT g.*, s.device_key AS meta_device_key, s.split AS meta_split,
       COUNT(DISTINCT g.udi_di) OVER (PARTITION BY g.g_mfr, g.g_model) AS n_dis_for_model
FROM g
LEFT JOIN `__MODEL_DATASET__.device_split` AS s ON s.base_key = g.base_key
WHERE g.base_key IS NOT NULL
-- @part linked_all
CREATE OR REPLACE TABLE `__MODEL_DATASET__.linked_all` AS
WITH m AS (
  SELECT source_record_id AS mdr_report_key, device_seq, udi_di, EXTRACT(YEAR FROM record_date) AS yr,
         manufacturer_name_norm AS m_mfr, product_code AS m_pc, device_name_raw AS m_name,
         model_number_raw AS m_model_raw, catalog_number AS m_catalog_raw,
         {{ identifier_key(model_number_raw) }} AS m_model,
         {{ identifier_key(catalog_number) }} AS m_cat
  FROM `__MAUDE__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
m_dedup AS (
  SELECT m_mfr, m_model, m_cat, m_pc, udi_di, COUNT(*) AS n_dup_rows,
         ARRAY_AGG(STRUCT(mdr_report_key, device_seq, yr, m_name, m_model_raw, m_catalog_raw)
                   ORDER BY FARM_FINGERPRINT(CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), '')))
                   LIMIT 1)[OFFSET(0)] AS ex
  FROM m
  GROUP BY m_mfr, m_model, m_cat, m_pc, udi_di
),
joined AS (
  SELECT d.ex.mdr_report_key, d.ex.device_seq, d.udi_di, d.ex.yr,
         d.m_mfr, d.m_pc, d.ex.m_name, d.ex.m_model_raw, d.ex.m_catalog_raw, d.m_model, d.m_cat, d.n_dup_rows,
         g.g_mfr, g.g_pcs, g.g_name, g.g_model_raw, g.g_catalog_raw, g.g_model, g.g_cat,
         g.n_dis_for_model, g.meta_device_key, g.meta_split
  FROM m_dedup AS d
  JOIN `__MODEL_DATASET__.gudid_keyed` AS g USING (udi_di)
)
SELECT *,
       {{ match_kind(m, g) }} AS match_kind,
       -- the catalog agrees on its own (not the model field copied into the catalog field)
       (NOT {{ is_placeholder(m_cat) }} AND m_cat = g_cat) AS catalog_agrees
FROM joined
-- @part review_conflicts
CREATE OR REPLACE TABLE `__MODEL_DATASET__.review_conflicts` AS
SELECT * FROM `__MODEL_DATASET__.linked_all`
WHERE match_kind = 'conflict'
-- @part positives
CREATE OR REPLACE TABLE `__MODEL_DATASET__.positives` AS
SELECT * EXCEPT (rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
           PARTITION BY meta_device_key
           ORDER BY FARM_FINGERPRINT(CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), ''), '|', udi_di))
         ) AS rn
  FROM `__MODEL_DATASET__.linked_all`
  WHERE match_kind != 'conflict'
    -- E4: a model key shared by too many DIs proves nothing unless the catalog agrees too
    AND NOT (match_kind = 'same_model_key' AND NOT {{ is_distinctive(n_dis_for_model) }} AND NOT catalog_agrees)
)
WHERE rn <= __MAX_PER_DEVICE__
