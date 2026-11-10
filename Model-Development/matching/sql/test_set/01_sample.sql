-- Title: Stratified sample of the no-UDI pool for the hand-labeled test set
-- Check: Takes the MAUDE device rows with no usable UDI link (the query 14 pool), collapses them
--   to distinct (manufacturer, model key, catalog key, device name) tuples, puts each tuple in one
--   of 12 strata (candidate bucket x manufacturer size), and draws @per_stratum tuples per stratum
--   in a fixed pseudo-random order (FARM_FINGERPRINT with @seed), so the same seed gives the same
--   sample.
-- Why: The hand-labeled set is the only test of the rows the model is for (no UDI). Sampling
--   tuples, not rows, stops one device (e.g. model 8100, 994K rows) from filling the set. Equal
--   allocation gives every stratum enough rows for the bias slices (n >= 30); the stratum totals
--   are returned so that metrics can be weighted back to the pool.
-- Informs: Which MAUDE tuples get labeled; the weights for pool-level precision and recall.
WITH gudid_di AS (
  SELECT DISTINCT udi_di FROM `__GUDID__` WHERE udi_di IS NOT NULL
),
gudid_models AS (
  -- Same definition as query 14: GUDID DIs per exact (manufacturer, model key).
  SELECT manufacturer_name_norm AS mfr,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key,
         COUNT(DISTINCT udi_di) AS n_exact
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY mfr, model_key
),
mfr_rank AS (
  SELECT manufacturer_name_norm AS mfr,
         ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, manufacturer_name_norm) AS rk
  FROM `__MAUDE__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY manufacturer_name_norm
),
pool AS (
  SELECT
    t.source_record_id AS mdr_report_key,
    t.device_seq,
    t.manufacturer_name_norm AS mfr,
    t.manufacturer_name_raw,
    t.device_name_raw,
    t.model_number_raw,
    t.catalog_number,
    t.product_code,
    t.record_date,
    IF(NULLIF(TRIM(t.udi_di), '') IS NULL, 'no_udi', 'udi_not_primary_di') AS udi_status,
    UPPER(REGEXP_REPLACE(TRIM(t.model_number_raw), r'[\s\-_./]', '')) AS model_key_raw,
    UPPER(REGEXP_REPLACE(TRIM(t.catalog_number), r'[\s\-_./]', '')) AS catalog_key_raw
  FROM `__MAUDE__` AS t
  LEFT JOIN gudid_di AS d ON t.udi_di = d.udi_di
  WHERE d.udi_di IS NULL
),
keyed AS (
  -- Placeholder rule from the README (shared conventions): under 2 characters or a placeholder
  -- value means missing.
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
),
tuples AS (
  -- One row per distinct tuple. The example row is picked by the seed, so it is reproducible.
  SELECT
    mfr, model_key, catalog_key, name_key,
    COUNT(*) AS n_rows,
    COUNTIF(udi_status = 'udi_not_primary_di') AS n_rows_udi_not_primary,
    ARRAY_AGG(STRUCT(mdr_report_key, device_seq, manufacturer_name_raw, device_name_raw,
                     model_number_raw, catalog_number, product_code, record_date, udi_status)
              ORDER BY FARM_FINGERPRINT(CONCAT(@seed, '|', IFNULL(mdr_report_key, ''), '|',
                                               CAST(device_seq AS STRING)))
              LIMIT 1)[OFFSET(0)] AS ex
  FROM keyed
  -- A tuple with nothing in it cannot be labeled.
  WHERE COALESCE(mfr, model_key, catalog_key, name_key) IS NOT NULL
  GROUP BY mfr, model_key, catalog_key, name_key
),
stratified AS (
  SELECT
    t.*,
    r.rk AS mfr_rank,
    CASE WHEN t.model_key IS NULL THEN 'no_model'
         WHEN IFNULL(gm.n_exact, 0) = 0 THEN 'cand_0'
         WHEN gm.n_exact = 1 THEN 'cand_1'
         ELSE 'cand_2plus' END AS cand_bucket,
    CASE WHEN r.rk <= 10 THEN 'top_10'
         WHEN r.rk <= 100 THEN '11_100'
         ELSE '101_plus_or_missing' END AS mfr_size
  FROM tuples AS t
  LEFT JOIN gudid_models AS gm ON gm.mfr = t.mfr AND gm.model_key = t.model_key
  LEFT JOIN mfr_rank AS r ON r.mfr = t.mfr
),
ranked AS (
  SELECT
    s.*,
    SUBSTR(TO_HEX(MD5(CONCAT(IFNULL(s.mfr, ''), '|', IFNULL(s.model_key, ''), '|',
                             IFNULL(s.catalog_key, ''), '|', IFNULL(s.name_key, '')))), 1, 12)
      AS sample_id,
    COUNT(*) OVER w AS stratum_tuples,
    SUM(s.n_rows) OVER w AS stratum_rows,
    ROW_NUMBER() OVER (PARTITION BY s.cand_bucket, s.mfr_size
                       ORDER BY FARM_FINGERPRINT(CONCAT(@seed, '|', IFNULL(s.mfr, ''), '|',
                                IFNULL(s.model_key, ''), '|', IFNULL(s.catalog_key, ''), '|',
                                IFNULL(s.name_key, '')))) AS draw
  FROM stratified AS s
  WINDOW w AS (PARTITION BY s.cand_bucket, s.mfr_size)
)
SELECT
  sample_id,
  CONCAT(cand_bucket, '|', mfr_size) AS stratum,
  cand_bucket,
  mfr_size,
  draw,
  stratum_tuples,
  stratum_rows,
  n_rows,
  n_rows_udi_not_primary,
  mfr,
  model_key,
  catalog_key,
  name_key,
  mfr_rank,
  ex.mdr_report_key AS example_mdr_report_key,
  ex.device_seq AS example_device_seq,
  ex.manufacturer_name_raw,
  ex.device_name_raw,
  ex.model_number_raw,
  ex.catalog_number,
  ex.product_code,
  ex.record_date AS example_record_date,
  ex.udi_status AS example_udi_status
FROM ranked
WHERE draw <= @per_stratum
ORDER BY stratum, draw
