-- Title: Strata of the hand-label frame: test-split no-UDI MAUDE rows per stratum (N5)
-- Check: Takes query 14's no-UDI pool (no udi_di, or one that is not a GUDID primary DI), keeps rows
--   in the TEST split, and counts them per stratum. A row is hard when one of E3b prefix only, E4
--   non-distinctive key, E3 suffix sibling, E7 cross-field agreement, E5 alias needed (the key exists
--   only under an alias labeler), E6 placeholder or E9 kit wording applies (first match, in that
--   order); each reason is one stratum. Easy rows are stratified by cell: candidate bucket (no usable
--   model, or 0 / 1 / 2+ GUDID DIs with the same labeler and model key) x manufacturer size (top 10 /
--   11-100 / smaller, ranked by MAUDE rows). A row's split is the split of the GUDID device with the
--   same key; else the split macro on the MAUDE key; with no usable identifier, on
--   mdr_report_key|device_seq.
-- Why: Hand labels measure the no-UDI pool, and precision must never be measured on training devices.
--   Hard cases are over-sampled on purpose, so the stratum sizes are needed to weight results back to
--   the pool (meta_sample_weight = rows in the stratum / rows drawn from it).
-- Informs: The allocation of the 150 rows in make_hand_label_sheet.py (hard strata evenly, easy
--   strata in proportion), and the weights.
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
  -- each key with the sibling keys it has; the row's own catalog key is not a conflicting sibling
  SELECT g_mfr, g_model, ARRAY_AGG(other) AS others FROM (
    SELECT g_mfr, short_key AS g_model, long_key AS other FROM suffix_pairs
    UNION DISTINCT
    SELECT g_mfr, long_key, short_key FROM suffix_pairs
  )
  GROUP BY g_mfr, g_model
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
      WHEN NOT {{ is_placeholder(p.m_model) }} AND mg.n_dis IS NOT NULL
           AND EXISTS (SELECT 1 FROM UNNEST(hs.others) AS o WHERE o IS DISTINCT FROM p.m_cat)
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
-- One row per stratum. A hard reason spans many cells, so grouping by cell too would split it.
SELECT
  stratum_id,
  LOGICAL_AND(is_hard) AS is_hard,
  COUNT(*) AS n_rows,
  COUNTIF(udi_status = 'no_udi') AS n_no_udi,
  COUNTIF(split_source = 'gudid_device') AS n_split_from_gudid,
  COUNTIF(split_source = 'maude_key_hash') AS n_split_from_maude_key,
  COUNTIF(split_source = 'row_hash') AS n_split_from_row,
  COUNTIF(hard_case_reason = 'E3_suffix_sibling') AS n_e3,
  COUNTIF(hard_case_reason = 'E3b_prefix_only') AS n_e3b,
  COUNTIF(hard_case_reason = 'E4_non_distinctive') AS n_e4,
  COUNTIF(hard_case_reason = 'E7_cross_field') AS n_e7,
  COUNTIF(hard_case_reason = 'E5_alias_labeler') AS n_e5,
  COUNTIF(hard_case_reason = 'E9_kit_wording') AS n_e9,
  COUNTIF(hard_case_reason = 'E6_placeholder') AS n_e6
FROM cells
GROUP BY stratum_id
ORDER BY stratum_id
