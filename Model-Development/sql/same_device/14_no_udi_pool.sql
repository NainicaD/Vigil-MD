-- Title: The no-UDI pool and how ambiguous it is
-- Check: Takes MAUDE rows with no usable UDI link (no UDI, or a UDI that is not a GUDID primary
--   DI) and buckets them by: whether the model number is usable; how many distinct GUDID DIs share
--   the same (manufacturer_name_norm, model key) (0, 1, 2-5, 6+); manufacturer size (rank by MAUDE
--   row count: top 10, 11-100, 101-1000, rest); and year band (prod MAUDE starts in 2016).
-- Why: The hand-labeled test set is drawn from these rows. "0 candidates" rows need fuzzy or alias
--   matching; "1 candidate" rows are the easy case; "2+" rows are only resolvable if the definition is
--   model-level (query 05). The strata are the bias-check slices (model number present vs missing,
--   big vs small manufacturer).
-- Informs: Sampling frame and strata for the hand-labeled MATCH / NON_MATCH /
--   INSUFFICIENT_EVIDENCE set, and how often INSUFFICIENT_EVIDENCE should be expected.
WITH gudid_di AS (
  SELECT DISTINCT udi_di FROM `__GUDID__` WHERE udi_di IS NOT NULL
),
gudid_models AS (
  SELECT manufacturer_name_norm AS mfr,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key,
         COUNT(DISTINCT udi_di) AS n_candidates
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
m AS (
  SELECT
    t.manufacturer_name_norm AS mfr,
    UPPER(REGEXP_REPLACE(TRIM(t.model_number_raw), r'[\s\-_./]', '')) AS model_key,
    EXTRACT(YEAR FROM t.record_date) AS yr,
    IF(NULLIF(TRIM(t.udi_di), '') IS NULL, 'no_udi', 'udi_not_primary_di') AS udi_status
  FROM `__MAUDE__` AS t
  LEFT JOIN gudid_di AS d ON t.udi_di = d.udi_di
  WHERE d.udi_di IS NULL
),
labeled AS (
  SELECT
    m.udi_status,
    (m.model_key IS NOT NULL AND LENGTH(m.model_key) >= 2 AND NOT REGEXP_CONTAINS(m.model_key,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'))
      AS model_usable,
    IFNULL(gm.n_candidates, 0) AS n_candidates,
    CASE WHEN r.rk IS NULL THEN 'mfr_missing'
         WHEN r.rk <= 10 THEN 'top_10'
         WHEN r.rk <= 100 THEN '11_100'
         WHEN r.rk <= 1000 THEN '101_1000'
         ELSE '1001_plus' END AS mfr_size,
    CASE WHEN m.yr IS NULL THEN 'unknown'
         WHEN m.yr < 2016 THEN 'pre_2016'
         WHEN m.yr < 2020 THEN '2016_2019'
         ELSE '2020_plus' END AS year_band
  FROM m
  LEFT JOIN gudid_models AS gm ON gm.mfr = m.mfr AND gm.model_key = m.model_key
  LEFT JOIN mfr_rank AS r ON r.mfr = m.mfr
)
SELECT
  udi_status,
  model_usable,
  CASE WHEN NOT model_usable THEN 'n/a'
       WHEN n_candidates = 0 THEN '0'
       WHEN n_candidates = 1 THEN '1'
       WHEN n_candidates <= 5 THEN '2-5'
       ELSE '6+' END AS gudid_candidates,
  mfr_size,
  year_band,
  COUNT(*) AS n_rows
FROM labeled
GROUP BY udi_status, model_usable, gudid_candidates, mfr_size, year_band
ORDER BY n_rows DESC
