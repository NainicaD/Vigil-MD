-- Title: Hard negatives: suffix siblings (E3a)
-- Check: For each positive whose MAUDE model key equals the GUDID model key, finds GUDID records of the
--   same labeler whose model key is that key plus or minus 1-3 trailing characters. Keeps pairs that
--   classify as prefix_related (so no identifier agrees) and sit in the anchor's split bucket. Caps
--   __NEG_PER_ANCHOR__ per anchor.
-- Why: GP200 vs GP200S looks near-identical to a string model but GUDID registers them as separate
--   devices (516,170 such key pairs, Q07). The anchor is itself a UDI-confirmed GP200S, so every
--   sibling is a proven NON_MATCH (E3a). A bare prefix with no anchor would be E3b, not a negative.
-- Informs: sql/labels/05_label_set.sql. Suffix siblings share a device stem, so they share a split;
--   the join still checks it, because the stem merge stops after 2 levels.
-- Placeholders filled by build_labels.py: __MODEL_DATASET__, __NEG_PER_ANCHOR__.
CREATE OR REPLACE TABLE `__MODEL_DATASET__.negatives_suffix` AS
WITH anchors AS (
  SELECT mdr_report_key, device_seq, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, udi_di AS anchor_udi_di, g_mfr AS anchor_g_mfr, g_model AS anchor_g_model,
         meta_split AS anchor_split
  FROM `__MODEL_DATASET__.positives`
  WHERE match_kind = 'same_model_key'
),
raw_cand AS (
  -- candidate key = anchor key plus k characters
  SELECT a.mdr_report_key, a.device_seq, a.yr, a.m_mfr, a.m_pc, a.m_name, a.m_model_raw, a.m_catalog_raw,
         a.m_model, a.m_cat, a.n_dup_rows, a.anchor_udi_di,
         c.udi_di, c.g_mfr, c.g_pcs, c.g_name, c.g_model_raw, c.g_catalog_raw, c.g_model, c.g_cat,
         c.n_dis_for_model, c.meta_device_key, c.meta_split
  FROM `__MODEL_DATASET__.gudid_keyed` AS c, UNNEST({{ suffix_lens() }}) AS k
  JOIN anchors AS a
    ON a.anchor_g_mfr = c.g_mfr
   AND a.anchor_g_model = SUBSTR(c.g_model, 1, LENGTH(c.g_model) - k)
   AND a.anchor_split = c.meta_split
  UNION ALL
  -- candidate key = anchor key minus k characters
  SELECT a.mdr_report_key, a.device_seq, a.yr, a.m_mfr, a.m_pc, a.m_name, a.m_model_raw, a.m_catalog_raw,
         a.m_model, a.m_cat, a.n_dup_rows, a.anchor_udi_di,
         c.udi_di, c.g_mfr, c.g_pcs, c.g_name, c.g_model_raw, c.g_catalog_raw, c.g_model, c.g_cat,
         c.n_dis_for_model, c.meta_device_key, c.meta_split
  FROM anchors AS a, UNNEST({{ suffix_lens() }}) AS k
  JOIN `__MODEL_DATASET__.gudid_keyed` AS c
    ON c.g_mfr = a.anchor_g_mfr
   AND c.g_model = SUBSTR(a.anchor_g_model, 1, LENGTH(a.anchor_g_model) - k)
   AND c.meta_split = a.anchor_split
),
classified AS (
  SELECT *, {{ match_kind(m, g) }} AS match_kind FROM raw_cand
)
SELECT * EXCEPT (rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
           PARTITION BY mdr_report_key, device_seq, anchor_udi_di
           ORDER BY FARM_FINGERPRINT(CONCAT(anchor_udi_di, '|', udi_di))
         ) AS rn
  FROM classified
  WHERE match_kind = 'prefix_related'
    AND NOT {{ is_placeholder(g_model) }}
)
WHERE rn <= __NEG_PER_ANCHOR__
