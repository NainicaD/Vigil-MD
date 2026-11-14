-- Title: Agree-region hard negatives (E1 in docs/EXPERIMENTS.md)
-- Check: For each UDI-linked positive, GUDID records that are NOT its device but whose identifiers still
--   agree with the MAUDE row, from three sources:
--   (a) cross_labeler: an exact identifier equality (model or catalog, either field) at a labeler that
--       is neither the anchor's GUDID labeler, the MAUDE labeler nor an alias of it, AND whose product
--       codes do not include the MAUDE product code (e.g. Dentsply catalog 24952 vs ResMed model 24952);
--   (b) contains: a `contains` substring hit at the anchor's labeler family and product code, on a
--       different model key (e.g. MAUDE 1260 inside GUDID H749195011260);
--   (c) non_distinctive: the anchor's own non-distinctive model key (more than 20 DIs) at the same
--       labeler, but both catalogs usable and different from the MAUDE and the anchor catalog.
--   Every candidate is in the anchor's split, has a different device key, and must still classify as
--   an agreeing kind. At most __AGREE_NEG_PER_ANCHOR__ per anchor; 0 builds an empty table (label_set_v1).
-- Why: 04 keeps only `conflict` negatives and 02 drops `conflict` positives, so no training negative has
--   agreeing identifiers and the match kind alone gives the label (diag D1: 0.9588; `contains` is 1,645
--   MATCH vs 0 NON_MATCH). The UDI proves these candidates are another device. Cross-labeler pairs with
--   the same product code are left out: the definition (SAME_DEVICE_DEFINITION.md section 1, rule 1)
--   still allows MATCH there, because the alias table is incomplete.
-- Informs: sql/labels/05_label_set.sql (label_source agree_<source>).
-- Placeholders filled by s01_build_label_set.py: __MODEL_DATASET__, __AGREE_NEG_PER_ANCHOR__.
CREATE OR REPLACE TABLE `__MODEL_DATASET__.negatives_agree` AS
WITH anchors AS (
  SELECT mdr_report_key, device_seq, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, udi_di AS anchor_udi_di, g_mfr AS anchor_g_mfr, g_model AS anchor_g_model,
         g_cat AS anchor_g_cat, meta_device_key AS anchor_device_key, meta_split AS anchor_split,
         CONCAT(mdr_report_key, '|', IFNULL(CAST(device_seq AS STRING), ''), '|', udi_di) AS anchor_key
  FROM `__MODEL_DATASET__.positives`
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
anchor_keys AS (
  SELECT a.*, k FROM anchors AS a, UNNEST([a.m_model, a.m_cat]) AS k
  WHERE NOT {{ is_placeholder(k) }}
),
gudid_keys AS (
  SELECT c.*, k FROM one_per_model AS c, UNNEST([c.g_model, c.g_cat]) AS k
  WHERE NOT {{ is_placeholder(k) }}
),
cross_labeler AS (
  SELECT a.* EXCEPT (k), c.* EXCEPT (k), 'cross_labeler' AS agree_source
  FROM anchor_keys AS a
  JOIN gudid_keys AS c ON c.k = a.k AND c.meta_split = a.anchor_split
  LEFT JOIN `__MODEL_DATASET__.alias_table` AS al ON al.maude_mfr = a.m_mfr AND al.gudid_mfr = c.g_mfr
  WHERE al.gudid_mfr IS NULL
    AND c.g_mfr != a.anchor_g_mfr
    AND c.g_mfr != IFNULL(a.m_mfr, '')
    AND a.m_pc IS NOT NULL AND ARRAY_LENGTH(c.g_pcs) > 0 AND a.m_pc NOT IN UNNEST(c.g_pcs)
),
models_by_pc AS (
  -- product code in the join key: without it a large labeler becomes anchors x all its models
  SELECT c.*, pc FROM one_per_model AS c, UNNEST(c.g_pcs) AS pc
),
contains_hit AS (
  SELECT a.*, c.* EXCEPT (pc), 'contains' AS agree_source
  FROM anchors AS a
  JOIN anchor_labelers AS l USING (anchor_key)
  JOIN models_by_pc AS c ON c.g_mfr = l.cand_mfr AND c.pc = a.m_pc AND c.meta_split = a.anchor_split
  WHERE c.g_model != a.anchor_g_model
    AND NOT {{ is_placeholder(a.m_model) }} AND LENGTH(a.m_model) >= 3 AND LENGTH(c.g_model) >= 3
    AND (STRPOS(c.g_model, a.m_model) > 0 OR STRPOS(a.m_model, c.g_model) > 0)
),
non_distinctive AS (
  SELECT a.*, c.*, 'non_distinctive' AS agree_source
  FROM anchors AS a
  JOIN `__MODEL_DATASET__.gudid_keyed` AS c
    ON c.g_mfr = a.anchor_g_mfr AND c.g_model = a.anchor_g_model AND c.meta_split = a.anchor_split
  WHERE NOT {{ is_distinctive(c.n_dis_for_model) }}
    AND NOT {{ is_placeholder(a.m_cat) }} AND NOT {{ is_placeholder(c.g_cat) }}
    AND c.g_cat != a.m_cat AND c.g_cat != IFNULL(a.anchor_g_cat, '')
),
raw_cand AS (
  SELECT mdr_report_key, device_seq, yr, m_mfr, m_pc, m_name, m_model_raw, m_catalog_raw, m_model, m_cat,
         n_dup_rows, anchor_udi_di, anchor_key, anchor_device_key,
         udi_di, g_mfr, g_pcs, g_name, g_model_raw, g_catalog_raw, g_model, g_cat,
         n_dis_for_model, meta_device_key, meta_split, agree_source
  FROM (SELECT * FROM cross_labeler UNION ALL SELECT * FROM contains_hit UNION ALL SELECT * FROM non_distinctive)
  WHERE udi_di != anchor_udi_di
    AND meta_device_key IS DISTINCT FROM anchor_device_key
),
classified AS (
  SELECT *, {{ match_kind(m, g) }} AS match_kind FROM raw_cand
),
agreeing AS (
  -- one row per (anchor, candidate) even when several keys or sources found it
  SELECT * FROM classified
  WHERE match_kind IN ('same_model_key', 'maude_model_eq_gudid_catalog', 'maude_catalog_eq_gudid_model',
                       'catalog_eq_catalog', 'contains')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY anchor_key, udi_di ORDER BY agree_source) = 1
)
SELECT * EXCEPT (rn) FROM (
  SELECT *, ROW_NUMBER() OVER (
           PARTITION BY anchor_key
           ORDER BY FARM_FINGERPRINT(CONCAT('agree|', anchor_udi_di, '|', udi_di))
         ) AS rn
  FROM agreeing
)
WHERE rn <= __AGREE_NEG_PER_ANCHOR__
