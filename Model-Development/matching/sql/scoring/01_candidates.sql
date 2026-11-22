-- Title: Candidate GUDID model groups for each MAUDE row of the no-UDI pool, for scoring
-- Check: Takes the MAUDE device rows with no usable UDI link (the query 14 pool) that changed
--   after @since (all rows when @since is NULL), and lists the GUDID model groups that five
--   rules of sql/test_set/02_candidates.sql propose: id_exact, id_contains, id_fuzzy,
--   name_same_mfr, name_any_mfr. random_same_mfr is not used: it adds control pairs for
--   labelers. One output row per (MAUDE row, GUDID model group), in the columns that
--   scripts/s12_score_pairs.py reads.
-- Why: The model scores pairs. This query gives it the pairs. The shared CTEs are copies of
--   the test-set query, so scoring sees the same candidates the hand-labeled set was drawn
--   with. tests/test_build_candidates.py checks that the copies stay identical.
-- Informs: vigil_md_model.match_candidates_<dataset> and the parquet file for s12 (@mode 'pool');
--   vigil_md_model.match_candidates_check_<dataset> for the weekly UDI agreement check
--   (@mode 'udi_check': rows with an exact UDI link, plus true_group_key).
WITH gudid_di AS (
  SELECT DISTINCT udi_di FROM `__GUDID__` WHERE udi_di IS NOT NULL
),
pool AS (
  SELECT
    t.source_record_id,
    t.device_seq,
    t.manufacturer_name_norm AS mfr,
    t.device_name_raw,
    t.model_number_raw,
    t.catalog_number,
    t.product_code,
    t.udi_di,
    UPPER(REGEXP_REPLACE(TRIM(t.model_number_raw), r'[\s\-_./]', '')) AS model_key_raw,
    UPPER(REGEXP_REPLACE(TRIM(t.catalog_number), r'[\s\-_./]', '')) AS catalog_key_raw
  FROM `__MAUDE__` AS t
  LEFT JOIN gudid_di AS d ON t.udi_di = d.udi_di
  WHERE (@since IS NULL OR t.last_updated_at > @since)
    -- 'pool': rows with no usable UDI link (the rows the model is for).
    -- 'udi_check': a fixed sample of rows WITH an exact UDI link, whose device is known; the
    -- model never sees the UDI (a banned feature), so these rows test it (s14_udi_agreement.py).
    AND IF(@mode = 'udi_check',
           d.udi_di IS NOT NULL
             AND MOD(ABS(FARM_FINGERPRINT(CONCAT(t.source_record_id, '|',
                                                 IFNULL(CAST(t.device_seq AS STRING), '')))), 10000)
                 < @sample_per_10k,
           d.udi_di IS NULL)
),
keyed AS (
  -- Placeholder rule of sql/test_set/01_sample.sql: under 2 characters or a placeholder
  -- value means missing. sample_id is the same tuple hash as in 01_sample.sql.
  SELECT
    k.*,
    SUBSTR(TO_HEX(MD5(CONCAT(IFNULL(k.mfr, ''), '|', IFNULL(k.model_key, ''), '|',
                             IFNULL(k.catalog_key, ''), '|', IFNULL(k.name_key, '')))), 1, 12)
      AS sample_id
  FROM (
    SELECT
      p.*,
      IF(p.model_key_raw IS NOT NULL AND LENGTH(p.model_key_raw) >= 2 AND NOT REGEXP_CONTAINS(p.model_key_raw,
           r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'),
         p.model_key_raw, NULL) AS model_key,
      IF(p.catalog_key_raw IS NOT NULL AND LENGTH(p.catalog_key_raw) >= 2 AND NOT REGEXP_CONTAINS(p.catalog_key_raw,
           r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'),
         p.catalog_key_raw, NULL) AS catalog_key,
      NULLIF(UPPER(TRIM(p.device_name_raw)), '') AS name_key
    FROM pool AS p
  ) AS k
),
s AS (
  SELECT DISTINCT sample_id, mfr, model_key, catalog_key, name_key
  FROM keyed
  WHERE COALESCE(mfr, model_key, catalog_key, name_key) IS NOT NULL
),
s_ids AS (
  SELECT sample_id, mfr, model_key AS id FROM s WHERE model_key IS NOT NULL
  UNION ALL
  SELECT sample_id, mfr, catalog_key AS id FROM s WHERE catalog_key IS NOT NULL
),
g_raw AS (
  SELECT
    udi_di, source_record_id, publish_date,
    manufacturer_name_norm AS mfr,
    manufacturer_name_raw, device_name_raw, model_number_raw, catalog_number, product_codes,
    device_record_status,
    NULLIF(UPPER(TRIM(device_name_raw)), '') AS name_key,
    UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS mk_raw,
    UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS ck_raw
  FROM `__GUDID__`
),
g AS (
  -- Placeholder rule from the README (shared conventions), on both identifier fields.
  SELECT
    r.* EXCEPT (mk_raw, ck_raw),
    IF(r.mk_raw IS NOT NULL AND LENGTH(r.mk_raw) >= 2 AND NOT REGEXP_CONTAINS(r.mk_raw,
         r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'),
       r.mk_raw, NULL) AS mk,
    IF(r.ck_raw IS NOT NULL AND LENGTH(r.ck_raw) >= 2 AND NOT REGEXP_CONTAINS(r.ck_raw,
         r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'),
       r.ck_raw, NULL) AS ck
  FROM g_raw AS r
),
g2 AS (
  SELECT g.*,
    IF(g.mk IS NOT NULL, CONCAT('M|', IFNULL(g.mfr, ''), '|', g.mk), CONCAT('DI|', g.udi_di))
      AS group_key
  FROM g
),
g_ids AS (
  SELECT group_key, mfr, mk AS id FROM g2 WHERE mk IS NOT NULL
  UNION ALL
  SELECT group_key, mfr, ck AS id FROM g2 WHERE ck IS NOT NULL
),
-- Rules 1 to 5: copies of sql/test_set/02_candidates.sql.
id_exact AS (
  SELECT sample_id, group_key, 'id_exact' AS source,
    ROW_NUMBER() OVER (PARTITION BY sample_id ORDER BY same_mfr DESC, group_key) AS rk
  FROM (
    SELECT DISTINCT si.sample_id, gi.group_key, (gi.mfr = si.mfr) AS same_mfr
    FROM s_ids AS si
    JOIN g_ids AS gi ON gi.id = si.id
    WHERE gi.mfr = si.mfr OR LENGTH(si.id) >= 5
  )
),
same_mfr_ids AS (
  SELECT si.sample_id, si.id AS s_id, gi.group_key, gi.id AS g_id
  FROM s_ids AS si
  JOIN g_ids AS gi ON gi.mfr = si.mfr
  WHERE gi.id != si.id
),
id_contains AS (
  SELECT sample_id, group_key, 'id_contains' AS source,
    ROW_NUMBER() OVER (PARTITION BY sample_id ORDER BY len_gap, group_key) AS rk
  FROM (
    SELECT sample_id, group_key, MIN(ABS(LENGTH(g_id) - LENGTH(s_id))) AS len_gap
    FROM same_mfr_ids
    WHERE LEAST(LENGTH(s_id), LENGTH(g_id)) >= 3
      AND (STRPOS(g_id, s_id) > 0 OR STRPOS(s_id, g_id) > 0)
    GROUP BY sample_id, group_key
  )
),
id_fuzzy AS (
  SELECT sample_id, group_key, 'id_fuzzy' AS source,
    ROW_NUMBER() OVER (PARTITION BY sample_id ORDER BY dist, group_key) AS rk
  FROM (
    SELECT sample_id, group_key, MIN(EDIT_DISTANCE(s_id, g_id)) AS dist
    FROM same_mfr_ids
    WHERE LENGTH(s_id) >= 4
      AND ABS(LENGTH(g_id) - LENGTH(s_id)) <= 2
      AND EDIT_DISTANCE(s_id, g_id) BETWEEN 1 AND 2
    GROUP BY sample_id, group_key
  )
),
name_same_mfr AS (
  SELECT sample_id, group_key, 'name_same_mfr' AS source,
    ROW_NUMBER() OVER (PARTITION BY sample_id
                       ORDER BY FARM_FINGERPRINT(CONCAT(@seed, sample_id, group_key))) AS rk
  FROM (
    SELECT DISTINCT s.sample_id, g2.group_key
    FROM s JOIN g2 ON g2.mfr = s.mfr AND g2.name_key = s.name_key
  )
),
name_any_mfr AS (
  SELECT sample_id, group_key, 'name_any_mfr' AS source,
    ROW_NUMBER() OVER (PARTITION BY sample_id
                       ORDER BY FARM_FINGERPRINT(CONCAT(@seed, sample_id, group_key))) AS rk
  FROM (
    SELECT DISTINCT s.sample_id, g2.group_key
    FROM s JOIN g2 ON g2.name_key = s.name_key
    WHERE LENGTH(s.name_key) >= 6 AND (s.mfr IS NULL OR g2.mfr IS NULL OR g2.mfr != s.mfr)
  )
),
pooled AS (
            SELECT * FROM id_exact        WHERE rk <= @cap_id_exact
  UNION ALL SELECT * FROM id_contains     WHERE rk <= @cap_other
  UNION ALL SELECT * FROM id_fuzzy        WHERE rk <= @cap_other
  UNION ALL SELECT * FROM name_same_mfr   WHERE rk <= @cap_other
  UNION ALL SELECT * FROM name_any_mfr    WHERE rk <= @cap_name_any
),
pairs AS (
  SELECT sample_id, group_key,
    ARRAY_AGG(source ORDER BY source) AS sources,
    MIN(rk) AS best_rank
  FROM pooled
  GROUP BY sample_id, group_key
),
model_groups AS (
  SELECT
    group_key,
    COUNT(*) AS n_dis,
    ARRAY_AGG(STRUCT(udi_di, mfr, manufacturer_name_raw, device_name_raw, model_number_raw,
                     catalog_number, product_codes, device_record_status)
              ORDER BY publish_date DESC, source_record_id LIMIT 1)[OFFSET(0)] AS rep,
    ARRAY_AGG(DISTINCT catalog_number IGNORE NULLS ORDER BY catalog_number LIMIT 5)
      AS catalog_examples
  FROM g2
  JOIN (SELECT DISTINCT group_key FROM pairs) USING (group_key)
  GROUP BY group_key
)
SELECT
  CONCAT(k.source_record_id, '|', IFNULL(CAST(k.device_seq AS STRING), '')) AS maude_id,
  gr.rep.udi_di AS gudid_id,
  gr.n_dis AS meta_n_dis_for_model,
  k.mfr AS key_m_mfr,
  k.device_name_raw AS key_m_name,
  k.model_number_raw AS key_m_model_raw,
  k.catalog_number AS key_m_catalog_raw,
  k.product_code AS key_m_pc,
  gr.rep.mfr AS key_g_mfr,
  gr.rep.device_name_raw AS key_g_name,
  gr.rep.model_number_raw AS key_g_model_raw,
  gr.rep.catalog_number AS key_g_catalog_raw,
  ARRAY_TO_STRING(gr.rep.product_codes, ';') AS key_g_pcs,
  ARRAY_TO_STRING(p.sources, ';') AS cand_sources,
  (SELECT COUNT(*) FROM pool) AS meta_n_pool_rows,
  p.group_key AS cand_group_key,
  tg.group_key AS true_group_key      -- the group of the row's own UDI; NULL in 'pool' mode
FROM keyed AS k
JOIN pairs AS p USING (sample_id)
JOIN model_groups AS gr USING (group_key)
-- MIN: a DI listed twice in GUDID must not double the row
LEFT JOIN (SELECT udi_di, MIN(group_key) AS group_key FROM g2 GROUP BY udi_di) AS tg
  ON tg.udi_di = k.udi_di
