-- Title: One model number, many GUDID DIs (size / pack / sterility variants)
-- Check: Groups GUDID by (manufacturer_name_norm, model key) for usable model keys (not a
--   placeholder, at least 2 characters). Part `distribution` buckets groups by how many distinct
--   DIs they hold. Part `largest` lists the 25 biggest groups; part `typical` lists 25 groups of
--   2-5 DIs picked by a stable hash, with their catalog numbers and device names so the reason
--   for the split (size, quantity, packaging, sterile vs non-sterile) can be read off.
-- Why: MAUDE usually reports a model number, not a DI. When one model has many DIs, the definition must say
--   whether linking to any of them is correct (model-level) or only one is (DI-level). This is
--   the main same-device decision: it sets what a positive label means and how many candidates a pair has.
-- Informs: Edge case "same model, different DI": MATCH at model level, or ambiguous at DI level.
-- @part distribution
WITH g AS (
  SELECT manufacturer_name_norm AS mfr, udi_di,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
),
usable AS (
  SELECT * FROM g
  WHERE mfr IS NOT NULL AND LENGTH(model_key) >= 2
    AND NOT REGEXP_CONTAINS(model_key,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
),
model_groups AS (
  SELECT mfr, model_key, COUNT(DISTINCT udi_di) AS n_di
  FROM usable GROUP BY mfr, model_key
)
SELECT
  CASE WHEN n_di = 1 THEN '1'
       WHEN n_di <= 5 THEN '2-5'
       WHEN n_di <= 20 THEN '6-20'
       WHEN n_di <= 100 THEN '21-100'
       ELSE '>100' END AS dis_per_model,
  COUNT(*) AS n_model_groups,
  SUM(n_di) AS n_dis,
  ROUND(SUM(n_di) / SUM(SUM(n_di)) OVER (), 4) AS share_of_dis
FROM model_groups
GROUP BY dis_per_model
ORDER BY MIN(n_di)
-- @part largest
WITH g AS (
  SELECT manufacturer_name_norm AS mfr, udi_di, catalog_number, device_name_raw, model_number_raw,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
)
SELECT
  mfr, model_key,
  COUNT(DISTINCT udi_di) AS n_di,
  COUNT(DISTINCT catalog_number) AS n_catalog,
  COUNT(DISTINCT device_name_raw) AS n_device_names,
  STRING_AGG(DISTINCT model_number_raw, ' | ' LIMIT 3) AS model_raw_examples,
  STRING_AGG(DISTINCT catalog_number, ' | ' LIMIT 5) AS catalog_examples,
  STRING_AGG(DISTINCT device_name_raw, ' | ' LIMIT 3) AS device_name_examples
FROM g
WHERE mfr IS NOT NULL AND LENGTH(model_key) >= 2
  AND NOT REGEXP_CONTAINS(model_key,
    r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
GROUP BY mfr, model_key
ORDER BY n_di DESC, mfr, model_key
LIMIT 25
-- @part typical
WITH g AS (
  SELECT manufacturer_name_norm AS mfr, udi_di, catalog_number, device_name_raw, model_number_raw,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
),
model_groups AS (
  SELECT
    mfr, model_key,
    COUNT(DISTINCT udi_di) AS n_di,
    COUNT(DISTINCT catalog_number) AS n_catalog,
    COUNT(DISTINCT device_name_raw) AS n_device_names,
    STRING_AGG(DISTINCT udi_di, ' | ' LIMIT 5) AS di_examples,
    STRING_AGG(DISTINCT catalog_number, ' | ' LIMIT 5) AS catalog_examples,
    STRING_AGG(DISTINCT device_name_raw, ' | ' LIMIT 3) AS device_name_examples
  FROM g
  WHERE mfr IS NOT NULL AND LENGTH(model_key) >= 2
    AND NOT REGEXP_CONTAINS(model_key,
      r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
  GROUP BY mfr, model_key
  HAVING n_di BETWEEN 2 AND 5
)
SELECT * FROM model_groups
ORDER BY FARM_FINGERPRINT(CONCAT(mfr, '|', model_key))
LIMIT 25
