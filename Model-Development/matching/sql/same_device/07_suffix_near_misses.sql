-- Title: Suffix near-misses (GP200 vs GP200S)
-- Check: Within one GUDID manufacturer, finds pairs of usable model keys where the longer key is
--   the shorter key plus 1-3 trailing characters (shorter key at least 3 characters and containing
--   a digit). Part `by_suffix` counts pairs by suffix and suffix class (letters / digits / mixed)
--   and how often the two keys carry different product codes; part `examples` lists 25 pairs with
--   device names on both sides.
-- Why: These are the conflicting-identifier cases the plan calls out. A string-similarity model
--   scores GP200 vs GP200S as near-identical. If GUDID registers them as different devices, the definition
--   must call them NON_MATCH and the model needs an explicit conflicting-identifier feature.
-- Informs: Edge case "suffix / revision difference": NON_MATCH; sizes the conflicting-identifier
--   feature and the hard negatives for the hand-labeled set.
-- @part by_suffix
WITH keys AS (
  SELECT manufacturer_name_norm AS mfr,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key,
         ANY_VALUE(product_codes[SAFE_OFFSET(0)]) AS pc
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY mfr, model_key
),
usable AS (
  SELECT * FROM keys
  WHERE LENGTH(model_key) >= 3
    AND NOT REGEXP_CONTAINS(model_key,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
),
pairs AS (
  SELECT b.mfr, a.model_key AS short_key, b.model_key AS long_key,
         SUBSTR(b.model_key, LENGTH(b.model_key) - k + 1) AS suffix,
         a.pc AS short_pc, b.pc AS long_pc
  FROM usable AS b, UNNEST([1, 2, 3]) AS k
  JOIN usable AS a
    ON a.mfr = b.mfr AND a.model_key = SUBSTR(b.model_key, 1, LENGTH(b.model_key) - k)
  WHERE REGEXP_CONTAINS(a.model_key, r'\d')
)
SELECT
  LENGTH(suffix) AS suffix_len,
  CASE WHEN REGEXP_CONTAINS(suffix, r'^[A-Z]+$') THEN 'letters'
       WHEN REGEXP_CONTAINS(suffix, r'^[0-9]+$') THEN 'digits'
       ELSE 'mixed' END AS suffix_class,
  suffix,
  COUNT(*) AS n_pairs,
  COUNT(DISTINCT mfr) AS n_manufacturers,
  COUNTIF(short_pc IS NOT NULL AND long_pc IS NOT NULL AND short_pc != long_pc) AS n_diff_product_code
FROM pairs
GROUP BY suffix_len, suffix_class, suffix
QUALIFY ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, suffix) <= 60
ORDER BY n_pairs DESC
-- @part totals
WITH keys AS (
  SELECT manufacturer_name_norm AS mfr,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY mfr, model_key
),
usable AS (
  SELECT * FROM keys
  WHERE LENGTH(model_key) >= 3
    AND NOT REGEXP_CONTAINS(model_key,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
),
pairs AS (
  SELECT b.mfr, a.model_key AS short_key, b.model_key AS long_key, k
  FROM usable AS b, UNNEST([1, 2, 3]) AS k
  JOIN usable AS a
    ON a.mfr = b.mfr AND a.model_key = SUBSTR(b.model_key, 1, LENGTH(b.model_key) - k)
  WHERE REGEXP_CONTAINS(a.model_key, r'\d')
)
SELECT
  (SELECT COUNT(*) FROM usable) AS n_usable_model_keys,
  COUNT(*) AS n_near_miss_pairs,
  COUNT(DISTINCT CONCAT(mfr, '|', short_key)) AS n_keys_with_longer_sibling,
  COUNT(DISTINCT mfr) AS n_manufacturers,
  COUNTIF(k = 1) AS n_suffix_len_1,
  COUNTIF(k = 2) AS n_suffix_len_2,
  COUNTIF(k = 3) AS n_suffix_len_3
FROM pairs
-- @part examples
WITH g AS (
  SELECT manufacturer_name_norm AS mfr, model_number_raw, device_name_raw,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
),
keys AS (
  SELECT mfr, model_key,
         ANY_VALUE(model_number_raw) AS model_raw,
         ANY_VALUE(device_name_raw) AS device_name
  FROM g
  WHERE LENGTH(model_key) >= 3
    AND NOT REGEXP_CONTAINS(model_key,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
  GROUP BY mfr, model_key
)
SELECT b.mfr, a.model_raw AS short_model, b.model_raw AS long_model,
       SUBSTR(b.model_key, LENGTH(b.model_key) - k + 1) AS suffix,
       a.device_name AS short_device_name, b.device_name AS long_device_name
FROM keys AS b, UNNEST([1, 2, 3]) AS k
JOIN keys AS a
  ON a.mfr = b.mfr AND a.model_key = SUBSTR(b.model_key, 1, LENGTH(b.model_key) - k)
WHERE REGEXP_CONTAINS(a.model_key, r'\d')
ORDER BY FARM_FINGERPRINT(CONCAT(b.mfr, '|', b.model_key))
LIMIT 25
