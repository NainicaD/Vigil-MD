-- Title: Hard and easy negatives from other models (E3b-safe)
-- Check: Part `hard`: for each positive, GUDID records of the same labeler (the anchor's GUDID labeler, the
--   MAUDE labeler, or an alias from the train-only alias table) that share the anchor's product code and
--   classify as `conflict` (different model key, not prefix- or contains-related, no catalog agreement).
--   Part `easy`: for a fixed share of positives, one random GUDID record of another labeler, also a
--   `conflict`. Every candidate sits in the anchor's split bucket.
-- Why: Prefix and containment cases are INSUFFICIENT_EVIDENCE (E3b), not NON_MATCH, so they must never
--   become negatives; requiring `conflict` guarantees that. Hard negatives teach the model that a shared
--   labeler and product code are not enough; easy negatives keep it from learning only that geometry.
--   A negative in a different split than its anchor would pair a test MAUDE row with a GUDID record
--   the model saw in training.
-- Informs: sql/labels/05_label_set.sql. Needs vigil_md_model.alias_table (maude_mfr, gudid_mfr).
-- Placeholders filled by build_labels.py: __MODEL_DATASET__, __NEG_PER_ANCHOR__, __CAND_POOL__ (models
--   kept per labeler and product code), __EASY_ANCHOR_PCT__ (% of positives that get an easy negative),
--   __EASY_POOL__ (GUDID records per split the easy draw picks from).
-- @part hard
CREATE OR REPLACE TABLE `__MODEL_DATASET__.negatives_same_labeler` AS
WITH anchors AS (
  SELECT mdr_report_key, device_seq, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, udi_di AS anchor_udi_di, g_mfr AS anchor_g_mfr, meta_split AS anchor_split,
         -- device_seq is nullable (E11), so a USING join on it would silently drop rows
         CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), ''), '|', udi_di) AS anchor_key
  FROM `__MODEL_DATASET__.positives`
  WHERE m_pc IS NOT NULL
),
anchor_labelers AS (
  SELECT anchor_key, anchor_g_mfr AS cand_mfr FROM anchors
  UNION DISTINCT
  SELECT anchor_key, m_mfr FROM anchors WHERE m_mfr IS NOT NULL
  UNION DISTINCT
  SELECT a.anchor_key, al.gudid_mfr
  FROM anchors AS a JOIN `__MODEL_DATASET__.alias_table` AS al ON al.maude_mfr = a.m_mfr
),
one_per_model AS (
  SELECT * FROM `__MODEL_DATASET__.gudid_keyed`
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY g_mfr, g_model ORDER BY FARM_FINGERPRINT(udi_di)) = 1
),
pool AS (
  SELECT * EXCEPT (rn) FROM (
    SELECT c.*, pc, ROW_NUMBER() OVER (PARTITION BY c.g_mfr, pc ORDER BY FARM_FINGERPRINT(c.udi_di)) AS rn
    FROM one_per_model AS c, UNNEST(c.g_pcs) AS pc
  )
  WHERE rn <= __CAND_POOL__
),
raw_cand AS (
  SELECT a.mdr_report_key, a.device_seq, a.yr, a.m_mfr, a.m_pc, a.m_name, a.m_model_raw, a.m_catalog_raw,
         a.m_model, a.m_cat, a.n_dup_rows, a.anchor_udi_di, a.anchor_key,
         p.udi_di, p.g_mfr, p.g_pcs, p.g_name, p.g_model_raw, p.g_catalog_raw, p.g_model, p.g_cat,
         p.n_dis_for_model, p.meta_device_key, p.meta_split
  FROM anchors AS a
  JOIN anchor_labelers AS l USING (anchor_key)
  JOIN pool AS p ON p.g_mfr = l.cand_mfr AND p.pc = a.m_pc AND p.meta_split = a.anchor_split
),
classified AS (
  SELECT *, {{ match_kind(m, g) }} AS match_kind FROM raw_cand
)
SELECT * EXCEPT (rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
           PARTITION BY anchor_key
           ORDER BY FARM_FINGERPRINT(CONCAT(anchor_udi_di, '|', udi_di))
         ) AS rn
  FROM classified
  WHERE match_kind = 'conflict'
)
WHERE rn <= __NEG_PER_ANCHOR__
-- @part easy
CREATE OR REPLACE TABLE `__MODEL_DATASET__.negatives_easy` AS
WITH anchors AS (
  SELECT mdr_report_key, device_seq, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, udi_di AS anchor_udi_di, g_mfr AS anchor_g_mfr, meta_split AS anchor_split,
         CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), ''), '|', udi_di) AS anchor_key
  FROM `__MODEL_DATASET__.positives`
  WHERE MOD(ABS(FARM_FINGERPRINT(CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), ''), '|', udi_di))), 100)
        < __EASY_ANCHOR_PCT__
),
pool AS (
  SELECT *, MAX(rn) OVER (PARTITION BY meta_split) AS n_pool FROM (
    SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.meta_split ORDER BY FARM_FINGERPRINT(c.udi_di)) AS rn
    FROM `__MODEL_DATASET__.gudid_keyed` AS c
  )
  WHERE rn <= __EASY_POOL__
),
raw_cand AS (
  -- each anchor draws one pool slot by hash, so the same few records are not everyone's easy negative
  SELECT a.mdr_report_key, a.device_seq, a.yr, a.m_mfr, a.m_pc, a.m_name, a.m_model_raw, a.m_catalog_raw,
         a.m_model, a.m_cat, a.n_dup_rows, a.anchor_udi_di,
         p.udi_di, p.g_mfr, p.g_pcs, p.g_name, p.g_model_raw, p.g_catalog_raw, p.g_model, p.g_cat,
         p.n_dis_for_model, p.meta_device_key, p.meta_split
  FROM anchors AS a
  JOIN pool AS p
    ON p.meta_split = a.anchor_split
   AND p.rn = 1 + MOD(ABS(FARM_FINGERPRINT(CONCAT('easy|', a.anchor_key))), p.n_pool)
  LEFT JOIN `__MODEL_DATASET__.alias_table` AS al ON al.maude_mfr = a.m_mfr AND al.gudid_mfr = p.g_mfr
  WHERE al.gudid_mfr IS NULL
    AND p.g_mfr != a.anchor_g_mfr
    AND p.g_mfr != IFNULL(a.m_mfr, '')
),
classified AS (
  SELECT *, {{ match_kind(m, g) }} AS match_kind FROM raw_cand
)
SELECT * FROM classified WHERE match_kind = 'conflict'
