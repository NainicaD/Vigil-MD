-- Title: Up to 3 ranked GUDID candidates for each drawn hand-label row (N5)
-- Check: For each row in @rows, GUDID devices of EITHER split proposed by four rules, best rule
--   first: (1) a MAUDE model or catalog key equals a GUDID model or catalog key and the labeler is the
--   same or an alias from alias_table; (2) the same with a key of 5+ characters under any labeler;
--   (3) same labeler, one key contains the other (suffix variants E3, prefix only E3b); (4) same
--   labeler and the same device name (rows with no usable identifier, E6). One DI stands for each
--   (labeler, model key): the newest. Keeps the first @max_cands per row and adds the columns the
--   labeler reads: match kind, distinctiveness, suffix sibling, raw identifier equality, and the
--   candidate's split (cand_split).
-- Why: About 60% of the pool has no exact GUDID candidate, so the sheet shows a short ranked list per
--   row plus a "none of these" row (added in Python). Candidates come from rules, not from any
--   model, so the hand labels favour no track. The MAUDE rows are test-split only (01/02), but
--   candidates come from both splits: a row's true device often has another key (alias, catalog in
--   the model field) that hashes to train, and hiding it would turn a real match into "none of
--   these". cand_split lets a report restrict to test-split candidates.
-- Informs: The candidate side of labels/hand_label_sample_v<batch>.csv.
-- Placeholders filled by make_hand_label_sheet.py: __MODEL_DATASET__. No regex literals: rules are macros.
WITH r AS (
  SELECT * FROM UNNEST(@rows)
),
r_ids AS (
  SELECT maude_row_id, m_mfr, m_model AS id FROM r WHERE NOT {{ is_placeholder(m_model) }}
  UNION ALL
  SELECT maude_row_id, m_mfr, m_cat AS id FROM r WHERE NOT {{ is_placeholder(m_cat) }}
),
g AS (
  SELECT
    udi_di, source_record_id, publish_date,
    manufacturer_name_norm AS g_mfr, manufacturer_name_raw AS g_mfr_raw, device_name_raw AS g_name,
    model_number_raw AS g_model_raw, catalog_number AS g_catalog_raw, product_codes AS g_pcs,
    {{ identifier_key(model_number_raw) }} AS g_model,
    {{ identifier_key(catalog_number) }} AS g_cat,
    {{ device_key(manufacturer_name_norm, model_number_raw, catalog_number) }} AS base_key
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
),
g_test AS (
  -- One record per (labeler, model key), from both splits; a record with no model key stands alone.
  -- (The name is kept for the rules below; cand_split says which split each candidate is in.)
  SELECT g.*, s.split AS g_split,
         COUNT(*) OVER (PARTITION BY g.g_mfr, IFNULL(g.g_model, g.udi_di)) AS n_dis_for_model
  FROM g
  LEFT JOIN `__MODEL_DATASET__.device_split` AS s USING (base_key)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY g.g_mfr, IFNULL(g.g_model, g.udi_di)
                             ORDER BY g.publish_date DESC, g.source_record_id) = 1
),
g_ids AS (
  SELECT udi_di, g_mfr, g_model AS id FROM g_test WHERE NOT {{ is_placeholder(g_model) }}
  UNION ALL
  SELECT udi_di, g_mfr, g_cat AS id FROM g_test WHERE NOT {{ is_placeholder(g_cat) }}
),
aliases AS (
  SELECT maude_mfr, gudid_mfr FROM `__MODEL_DATASET__.alias_table`
),
model_groups AS (
  SELECT manufacturer_name_norm AS g_mfr, {{ usable_key(model_number_raw) }} AS g_model
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY g_mfr, g_model
  HAVING g_model IS NOT NULL
),
suffix_pairs AS (
  SELECT l.g_mfr, s.g_model AS short_key, l.g_model AS long_key
  FROM model_groups AS l, UNNEST({{ suffix_lens() }}) AS d
  JOIN model_groups AS s
    ON s.g_mfr = l.g_mfr AND s.g_model = SUBSTR(l.g_model, 1, LENGTH(l.g_model) - d)
  WHERE LENGTH(l.g_model) > d
),
has_sibling AS (
  SELECT g_mfr AS sib_mfr, short_key AS sib_key FROM suffix_pairs
  UNION DISTINCT
  SELECT g_mfr, long_key FROM suffix_pairs
),
cands AS (
  -- rule 1: an identifier agrees, same labeler or a known alias
  SELECT ri.maude_row_id, gi.udi_di, 1 AS rule
  FROM r_ids AS ri
  JOIN g_ids AS gi ON gi.id = ri.id
  LEFT JOIN aliases AS a ON a.maude_mfr = ri.m_mfr AND a.gudid_mfr = gi.g_mfr
  WHERE gi.g_mfr = ri.m_mfr OR a.maude_mfr IS NOT NULL
  UNION ALL
  -- rule 2: an identifier of 5+ characters agrees, any other labeler
  SELECT ri.maude_row_id, gi.udi_di, 2 AS rule
  FROM r_ids AS ri
  JOIN g_ids AS gi ON gi.id = ri.id
  WHERE LENGTH(ri.id) >= 5 AND (ri.m_mfr IS NULL OR gi.g_mfr IS NULL OR gi.g_mfr != ri.m_mfr)
  UNION ALL
  -- rule 3: same labeler, one identifier contains the other (E3 suffix variants, E3b prefix only)
  SELECT ri.maude_row_id, gi.udi_di, 3 AS rule
  FROM r_ids AS ri
  JOIN g_ids AS gi ON gi.g_mfr = ri.m_mfr
  WHERE gi.id != ri.id AND LEAST(LENGTH(gi.id), LENGTH(ri.id)) >= 3
    AND (STRPOS(gi.id, ri.id) > 0 OR STRPOS(ri.id, gi.id) > 0)
  UNION ALL
  -- rule 4: same labeler, same device name (case and outer spaces ignored)
  SELECT r.maude_row_id, gt.udi_di, 4 AS rule
  FROM r
  JOIN g_test AS gt ON gt.g_mfr = r.m_mfr
  WHERE NULLIF(TRIM(r.m_name), '') IS NOT NULL AND LOWER(TRIM(r.m_name)) = LOWER(TRIM(gt.g_name))
),
best AS (
  SELECT maude_row_id, udi_di, MIN(rule) AS rule
  FROM cands
  GROUP BY maude_row_id, udi_di
),
ranked AS (
  SELECT b.*,
         ROW_NUMBER() OVER (PARTITION BY b.maude_row_id
                            ORDER BY b.rule, FARM_FINGERPRINT(CONCAT(@salt, '|', b.udi_di))) AS cand_rank
  FROM best AS b
)
SELECT
  x.maude_row_id,
  x.cand_rank,
  x.rule AS cand_rule,
  IFNULL(gt.g_split, 'none') AS cand_split,
  gt.udi_di AS cand_udi_di,
  gt.g_mfr AS cand_labeler,
  gt.g_mfr_raw AS cand_labeler_raw,
  gt.g_name AS cand_device_name,
  gt.g_model_raw AS cand_model,
  gt.g_catalog_raw AS cand_catalog,
  ARRAY_TO_STRING(gt.g_pcs, ';') AS cand_product_codes,
  gt.n_dis_for_model AS cand_n_dis_for_model,
  {{ is_distinctive(gt.n_dis_for_model) }} AS cand_is_distinctive,
  {{ match_kind(m, g) }} AS cand_match_kind,
  hs.sib_key IS NOT NULL AS cand_has_suffix_sibling,
  ({{ raw_equal(r.m_model_raw, gt.g_model_raw) }} OR {{ raw_equal(r.m_model_raw, gt.g_catalog_raw) }}
   OR {{ raw_equal(r.m_catalog_raw, gt.g_model_raw) }} OR {{ raw_equal(r.m_catalog_raw, gt.g_catalog_raw) }})
    AS cand_raw_identifier_equal
FROM ranked AS x
JOIN r USING (maude_row_id)
JOIN g_test AS gt ON gt.udi_di = x.udi_di
LEFT JOIN has_sibling AS hs ON hs.sib_mfr = gt.g_mfr AND hs.sib_key = gt.g_model
WHERE x.cand_rank <= @max_cands
ORDER BY x.maude_row_id, x.cand_rank
