-- Title: Draw the hand-label rows: a fixed quota per cell, in a fixed order (N5)
-- Check: The same frame and cells as 01_cells.sql. From each cell in @quotas it takes the first n rows
--   ordered by FARM_FINGERPRINT(mdr_report_key|device_seq|@salt), after removing the rows of earlier
--   batches (@exclude_row_ids). Returns the MAUDE side of each drawn row.
-- Why: Deterministic: the same salt and data give the same rows. Batch 2 uses a new salt and excludes
--   batch 1. FARM_FINGERPRINT is used for ordering only, where Python never needs to recompute it.
-- Informs: The MAUDE side of the sheet, and the rows 03_candidates.sql looks up.
-- Placeholders filled by make_hand_label_sheet.py: __MODEL_DATASET__. No regex literals: rules are macros.
WITH
-- BEGIN frame (keep identical in 01 and 02: the cell counts and the draw must see the same rows)
gudid_di AS (
  SELECT DISTINCT udi_di FROM `__GUDID__` WHERE udi_di IS NOT NULL
),
model_groups AS (
  -- GUDID DIs per (labeler, usable model key)
  SELECT manufacturer_name_norm AS g_mfr, {{ usable_key(model_number_raw) }} AS g_model,
         COUNT(DISTINCT udi_di) AS n_dis
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY g_mfr, g_model
  HAVING g_model IS NOT NULL
),
suffix_pairs AS (
  -- keys of one labeler where the long one is the short one plus 1-3 characters (E3, query 07's join)
  SELECT l.g_mfr, s.g_model AS short_key, l.g_model AS long_key
  FROM model_groups AS l, UNNEST({{ suffix_lens() }}) AS d
  JOIN model_groups AS s
    ON s.g_mfr = l.g_mfr AND s.g_model = SUBSTR(l.g_model, 1, LENGTH(l.g_model) - d)
  WHERE LENGTH(l.g_model) > d
),
has_sibling AS (
  SELECT g_mfr, short_key AS g_model FROM suffix_pairs
  UNION DISTINCT
  SELECT g_mfr, long_key FROM suffix_pairs
),
extended_stems AS (
  -- (labeler, X) where the labeler has a key X plus 1-3 characters, whether or not X itself exists (E3b)
  SELECT DISTINCT g.g_mfr, SUBSTR(g.g_model, 1, LENGTH(g.g_model) - d) AS stem
  FROM model_groups AS g, UNNEST({{ suffix_lens() }}) AS d
  WHERE LENGTH(g.g_model) > d
),
alias_models AS (
  -- (MAUDE labeler, model key) pairs where the key exists under one of the labeler's GUDID aliases
  SELECT DISTINCT a.maude_mfr, mg.g_model
  FROM `__MODEL_DATASET__.alias_table` AS a
  JOIN model_groups AS mg ON mg.g_mfr = a.gudid_mfr
),
mfr_rank AS (
  SELECT manufacturer_name_norm AS mfr,
         ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, manufacturer_name_norm) AS rk
  FROM `__MAUDE__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY manufacturer_name_norm
),
pool AS (
  -- Query 14's pool: no udi_di, or one that is not a GUDID primary DI
  SELECT
    t.source_record_id AS mdr_report_key,
    t.device_seq,
    t.manufacturer_name_norm AS m_mfr,
    t.manufacturer_name_raw AS m_mfr_raw,
    t.device_name_raw AS m_name,
    t.model_number_raw AS m_model_raw,
    t.catalog_number AS m_catalog_raw,
    t.product_code AS m_pc,
    t.record_date AS m_date,
    IF(NULLIF(TRIM(t.udi_di), '') IS NULL, 'no_udi', 'udi_not_primary_di') AS udi_status,
    -- evidence for the human labeler only, never a feature
    IF(NULLIF(TRIM(t.udi_di), '') IS NULL, NULL, t.udi_di) AS m_udi_di_raw,
    {{ identifier_key(t.model_number_raw) }} AS m_model,
    {{ identifier_key(t.catalog_number) }} AS m_cat,
    {{ device_key(t.manufacturer_name_norm, t.model_number_raw, t.catalog_number) }} AS m_base_key
  FROM `__MAUDE__` AS t
  LEFT JOIN gudid_di AS d ON t.udi_di = d.udi_di
  WHERE d.udi_di IS NULL
),
frame AS (
  SELECT
    p.*,
    SUBSTR(TO_HEX(SHA256(CONCAT(p.mdr_report_key, '|', IFNULL(CAST(p.device_seq AS STRING), '')))), 1, 16)
      AS maude_row_id,
    NOT {{ is_placeholder(p.m_model) }} AS model_usable,
    IFNULL(mg.n_dis, 0) AS n_exact,
    CASE WHEN r.rk <= 10 THEN 'top_10' WHEN r.rk <= 100 THEN '11_100' ELSE 'smaller' END AS mfr_size,
    -- The split of the GUDID device with the same key; else the hash of the MAUDE key; with no usable
    -- identifier, the hash of the report row (nothing to memorize, never in training).
    COALESCE(ds.split,
             IF(p.m_base_key IS NOT NULL, {{ split(p.m_base_key) }}, NULL),
             {{ split(CONCAT(p.mdr_report_key, '|', IFNULL(CAST(p.device_seq AS STRING), ''))) }}) AS frame_split,
    CASE WHEN ds.split IS NOT NULL THEN 'gudid_device'
         WHEN p.m_base_key IS NOT NULL THEN 'maude_key_hash'
         ELSE 'row_hash' END AS split_source,
    -- The first hard-case reason that applies, most specific first. E5 only when the alias is what
    -- makes a match possible: the key exists under an alias labeler but not under the MAUDE name.
    CASE
      WHEN NOT {{ is_placeholder(p.m_model) }} AND mg.n_dis IS NULL AND es.stem IS NOT NULL
        THEN 'E3b_prefix_only'
      WHEN NOT {{ is_placeholder(p.m_model) }} AND mg.n_dis IS NOT NULL AND NOT {{ is_distinctive(mg.n_dis) }}
        THEN 'E4_non_distinctive'
      WHEN NOT {{ is_placeholder(p.m_model) }} AND mg.n_dis IS NOT NULL AND hs.g_model IS NOT NULL
        THEN 'E3_suffix_sibling'
      WHEN NOT {{ is_placeholder(p.m_cat) }} AND mg.n_dis IS NULL AND cx.n_dis IS NOT NULL
        THEN 'E7_cross_field'
      WHEN NOT {{ is_placeholder(p.m_model) }} AND mg.n_dis IS NULL AND am.g_model IS NOT NULL
        THEN 'E5_alias_labeler'
      WHEN NULLIF(TRIM(p.m_model_raw), '') IS NOT NULL AND {{ is_placeholder(p.m_model) }} THEN 'E6_placeholder'
      WHEN {{ has_kit_word(p.m_name) }} THEN 'E9_kit_wording'
    END AS hard_case_reason
  FROM pool AS p
  LEFT JOIN model_groups AS mg ON mg.g_mfr = p.m_mfr AND mg.g_model = p.m_model
  LEFT JOIN has_sibling AS hs ON hs.g_mfr = p.m_mfr AND hs.g_model = p.m_model
  LEFT JOIN extended_stems AS es ON es.g_mfr = p.m_mfr AND es.stem = p.m_model
  LEFT JOIN model_groups AS cx ON cx.g_mfr = p.m_mfr AND cx.g_model = p.m_cat
  LEFT JOIN alias_models AS am ON am.maude_mfr = p.m_mfr AND am.g_model = p.m_model
  LEFT JOIN mfr_rank AS r ON r.mfr = p.m_mfr
  LEFT JOIN `__MODEL_DATASET__.device_split` AS ds ON ds.base_key = p.m_base_key
),
stratified AS (
  SELECT
    f.*,
    CASE WHEN NOT f.model_usable THEN 'no_model'
         WHEN f.n_exact = 0 THEN 'cand_0'
         WHEN f.n_exact = 1 THEN 'cand_1'
         ELSE 'cand_2plus' END AS cand_bucket,
    f.hard_case_reason IS NOT NULL AS is_hard
  FROM frame AS f
  WHERE f.frame_split = 'test'
),
cells AS (
  -- Hard rows: one stratum per reason (allocated evenly). Easy rows: one per cell (proportional).
  SELECT s.*,
         IF(s.is_hard, CONCAT(s.hard_case_reason, '|hard'), CONCAT(s.cand_bucket, '|', s.mfr_size, '|easy'))
           AS stratum_id
  FROM stratified AS s
)
-- END frame
,
quotas AS (
  SELECT * FROM UNNEST(@quotas)
),
excluded AS (
  SELECT DISTINCT id FROM UNNEST(@exclude_row_ids) AS id
)
SELECT
  c.maude_row_id, c.mdr_report_key, c.device_seq, c.stratum_id, c.cand_bucket, c.mfr_size, c.is_hard,
  c.hard_case_reason, c.udi_status, c.model_usable, c.split_source, c.n_exact,
  c.m_mfr, c.m_mfr_raw, c.m_name, c.m_model_raw, c.m_catalog_raw, c.m_model, c.m_cat, c.m_pc, c.m_date,
  c.m_udi_di_raw
FROM cells AS c
JOIN quotas AS q ON q.stratum_id = c.stratum_id
LEFT JOIN excluded AS e ON e.id = c.maude_row_id
WHERE e.id IS NULL
QUALIFY ROW_NUMBER() OVER (
          PARTITION BY c.stratum_id
          ORDER BY FARM_FINGERPRINT(CONCAT(c.mdr_report_key, '|', IFNULL(CAST(c.device_seq AS STRING), ''), '|', @salt))
        ) <= q.n
ORDER BY c.stratum_id, c.maude_row_id
