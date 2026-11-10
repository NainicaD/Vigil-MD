-- Title: Candidate GUDID model groups for each sampled MAUDE tuple (pooled from several rules)
-- Check: For each tuple in @sample, lists GUDID model groups proposed by any of six rules:
--   id_exact (a MAUDE model or catalog key equals a GUDID model or catalog key; any manufacturer
--   if the key has 5+ characters), id_contains (same manufacturer, one key contains the other),
--   id_fuzzy (same manufacturer, edit distance 1-2), name_same_mfr (same manufacturer and device
--   name), name_any_mfr (same device name, any manufacturer), random_same_mfr (two random groups
--   of the same manufacturer, as controls). A model group is (manufacturer, model key), at the
--   model level of SAME_DEVICE_DEFINITION.md section 1; a GUDID record with no usable model key is
--   its own group. Each group is shown by its newest DI.
-- Why: Labelers need a short list to judge. Taking the list from several rules, not from one
--   model, keeps the test set from favouring any track: a true match that one track's retrieval
--   misses is still in the list if another rule finds it. The rules follow E2 (formatting), E3
--   (suffix variants, the hard negatives), E5 (aliases: id_exact across manufacturers) and E7
--   (model and catalog swapped).
-- Informs: The pair rows of the labeling sheet. Embedding candidates are added to the same sheet
--   later, and only the new pairs are labeled.
WITH s AS (
  SELECT * FROM UNNEST(@sample)
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
-- Rule 1: exact identifier key, any field against any field (E7). Across manufacturers only for
-- keys of 5+ characters, so short keys like "100" do not pull in every labeler (aliases, E5).
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
-- Rule 2: containment within one manufacturer: prefixes and suffix variants (E3, E3b) and
-- names around a model (E2).
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
-- Rule 3: typos within one manufacturer.
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
-- Rules 4 and 5: device (brand) name, for tuples with no usable identifier (E6).
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
-- Rule 6: random groups of the same manufacturer. They are mostly easy NON_MATCHes and check that
-- labelers do not say MATCH to everything.
random_same_mfr AS (
  SELECT sample_id, group_key, 'random_same_mfr' AS source,
    ROW_NUMBER() OVER (PARTITION BY sample_id
                       ORDER BY FARM_FINGERPRINT(CONCAT(@seed, 'r', sample_id, group_key))) AS rk
  FROM (
    SELECT DISTINCT s.sample_id, g2.group_key
    FROM s JOIN g2 ON g2.mfr = s.mfr
  )
),
pooled AS (
            SELECT * FROM id_exact        WHERE rk <= @cap_id_exact
  UNION ALL SELECT * FROM id_contains     WHERE rk <= @cap_other
  UNION ALL SELECT * FROM id_fuzzy        WHERE rk <= @cap_other
  UNION ALL SELECT * FROM name_same_mfr   WHERE rk <= @cap_other
  UNION ALL SELECT * FROM name_any_mfr    WHERE rk <= @cap_name_any
  UNION ALL SELECT * FROM random_same_mfr WHERE rk <= @cap_random
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
  p.sample_id,
  p.group_key,
  ARRAY_TO_STRING(p.sources, ';') AS sources,
  p.best_rank,
  gr.n_dis,
  gr.rep.udi_di AS rep_udi_di,
  gr.rep.mfr AS gudid_mfr,
  gr.rep.manufacturer_name_raw AS gudid_manufacturer_name_raw,
  gr.rep.device_name_raw AS gudid_device_name_raw,
  gr.rep.model_number_raw AS gudid_model_number_raw,
  gr.rep.catalog_number AS gudid_catalog_number,
  ARRAY_TO_STRING(gr.catalog_examples, ';') AS gudid_catalog_examples,
  ARRAY_TO_STRING(gr.rep.product_codes, ';') AS gudid_product_codes,
  gr.rep.device_record_status AS gudid_device_record_status
FROM pairs AS p
JOIN model_groups AS gr USING (group_key)
ORDER BY p.sample_id, p.best_rank, p.group_key
