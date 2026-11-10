-- Title: Placeholder-model negatives (E3 in docs/EXPERIMENTS.md)
-- Check: For each UDI-linked positive of kind maude_model_missing_or_placeholder whose MAUDE catalog is
--   usable, GUDID records of another model at the anchor's labeler family (GUDID labeler, MAUDE labeler or
--   an alias) and product code. Each must still classify as maude_model_missing_or_placeholder (so the
--   MAUDE catalog equals none of its identifiers), and neither its model nor its catalog key may be a
--   prefix of the MAUDE catalog key or the other way round (71501 vs 71501-01 is INSUFFICIENT_EVIDENCE, not
--   NON_MATCH). In the anchor's split, a different device key, at most __PLACEHOLDER_NEG_PER_ANCHOR__ per
--   anchor; 0 builds an empty table (label_set_v1, label_set_v2_e1).
-- Why: every training row of this kind was MATCH (14,607 vs 0 in label_set_v2_e1), so the models accept a
--   missing model as evidence: diag D10 fp push of feat_maude_model_missing was positive in every model, and
--   the hand pairs MAUDE catalog 71501-01 vs candidate 70817 / 70819 (same labeler, same name) score 0.99999.
--   The UDI proves these candidates are another device, and the usable catalog conflicts, so they are
--   NON_MATCH under SAME_DEVICE_DEFINITION.md section 2. Anchors without a usable catalog are left out: with no
--   usable identifier the pair is INSUFFICIENT_EVIDENCE (E6), which the E4 gate handles.
-- Informs: sql/labels/05_label_set.sql (label_source placeholder_other_model).
-- Placeholders filled by s01_build_label_set.py: __MODEL_DATASET__, __PLACEHOLDER_NEG_PER_ANCHOR__.
CREATE OR REPLACE TABLE `__MODEL_DATASET__.negatives_placeholder` AS
WITH anchors AS (
  SELECT mdr_report_key, device_seq, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, udi_di AS anchor_udi_di, g_mfr AS anchor_g_mfr, g_model AS anchor_g_model,
         meta_device_key AS anchor_device_key, meta_split AS anchor_split,
         CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), ''), '|', udi_di) AS anchor_key
  FROM `__MODEL_DATASET__.positives`
  WHERE match_kind = 'maude_model_missing_or_placeholder'
    AND m_pc IS NOT NULL
    AND NOT {{ is_placeholder(m_cat) }}
),
anchor_labelers AS (
  SELECT anchor_key, anchor_g_mfr AS cand_mfr FROM anchors
  UNION DISTINCT
  SELECT anchor_key, m_mfr FROM anchors WHERE m_mfr IS NOT NULL
  UNION DISTINCT
  SELECT a.anchor_key, al.gudid_mfr
  FROM anchors AS a JOIN `__MODEL_DATASET__.alias_table` AS al ON al.maude_mfr = a.m_mfr
),
models_by_pc AS (
  -- one record per (labeler, model), product code in the join key (see 04b)
  SELECT c.*, pc FROM (
    SELECT * FROM `__MODEL_DATASET__.gudid_keyed`
    WHERE TRUE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY g_mfr, g_model ORDER BY FARM_FINGERPRINT(udi_di)) = 1
  ) AS c, UNNEST(c.g_pcs) AS pc
),
raw_cand AS (
  SELECT a.mdr_report_key, a.device_seq, a.yr, a.m_mfr, a.m_pc, a.m_name, a.m_model_raw, a.m_catalog_raw,
         a.m_model, a.m_cat, a.n_dup_rows, a.anchor_udi_di, a.anchor_key,
         c.udi_di, c.g_mfr, c.g_pcs, c.g_name, c.g_model_raw, c.g_catalog_raw, c.g_model, c.g_cat,
         c.n_dis_for_model, c.meta_device_key, c.meta_split
  FROM anchors AS a
  JOIN anchor_labelers AS l USING (anchor_key)
  JOIN models_by_pc AS c ON c.g_mfr = l.cand_mfr AND c.pc = a.m_pc AND c.meta_split = a.anchor_split
  WHERE c.udi_di != a.anchor_udi_di
    AND c.g_model IS DISTINCT FROM a.anchor_g_model
    AND c.meta_device_key IS DISTINCT FROM a.anchor_device_key
    -- a prefix relation between the MAUDE catalog and a candidate key is E3b (IE), not a negative
    AND NOT IFNULL(STARTS_WITH(a.m_cat, c.g_model) OR STARTS_WITH(c.g_model, a.m_cat), FALSE)
    AND NOT IFNULL(STARTS_WITH(a.m_cat, c.g_cat) OR STARTS_WITH(c.g_cat, a.m_cat), FALSE)
),
classified AS (
  SELECT *, {{ match_kind(m, g) }} AS match_kind FROM raw_cand
)
SELECT * EXCEPT (rn, anchor_key) FROM (
  SELECT *, ROW_NUMBER() OVER (
           PARTITION BY anchor_key
           ORDER BY FARM_FINGERPRINT(CONCAT('placeholder|', anchor_udi_di, '|', udi_di))
         ) AS rn
  FROM classified
  WHERE match_kind = 'maude_model_missing_or_placeholder'
)
WHERE rn <= __PLACEHOLDER_NEG_PER_ANCHOR__
