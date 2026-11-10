-- Title: Model numbers that differ only in formatting (IP-200 vs IP200)
-- Check: Within one manufacturer in GUDID, finds distinct raw model numbers (trimmed, case kept)
--   that collapse to the same model key once upper-cased and stripped of spaces and - _ . /.
--   Part `summary` counts such keys; part `examples` lists 25 of them with their raw spellings
--   and how many distinct DIs and product codes sit behind them.
-- Why: The blocking key in Data-Pipeline/scripts/matching/blocking.py already strips "-" and
--   spaces. If GUDID itself lists IP-200 and IP200 as different devices of one manufacturer,
--   that normalization would merge different devices. If not, it is safe.
-- Informs: Edge case "formatting-only difference": MATCH, and which characters the shared
--   feature function may strip.
-- @part summary
WITH g AS (
  SELECT manufacturer_name_norm AS mfr, TRIM(model_number_raw) AS model_raw, udi_di,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL AND NULLIF(TRIM(model_number_raw), '') IS NOT NULL
),
keys AS (
  SELECT mfr, model_key,
         COUNT(DISTINCT model_raw) AS n_raw,
         COUNT(DISTINCT UPPER(model_raw)) AS n_raw_upper
  FROM g WHERE LENGTH(model_key) >= 2
  GROUP BY mfr, model_key
)
SELECT
  COUNT(*) AS n_model_keys,
  COUNTIF(n_raw > 1) AS n_keys_with_variants,
  COUNTIF(n_raw_upper > 1) AS n_keys_with_punct_or_space_variants,
  COUNTIF(n_raw > 1 AND n_raw_upper = 1) AS n_keys_with_case_only_variants,
  ROUND(COUNTIF(n_raw > 1) / COUNT(*), 6) AS pct_keys_with_variants
FROM keys
-- @part examples
WITH g AS (
  SELECT manufacturer_name_norm AS mfr, TRIM(model_number_raw) AS model_raw, udi_di,
         device_name_raw, product_codes,
         UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS model_key
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL AND NULLIF(TRIM(model_number_raw), '') IS NOT NULL
),
keys AS (
  SELECT mfr, model_key,
         COUNT(DISTINCT UPPER(model_raw)) AS n_raw_upper,
         COUNT(DISTINCT udi_di) AS n_di,
         STRING_AGG(DISTINCT model_raw, ' | ' LIMIT 5) AS raw_spellings,
         STRING_AGG(DISTINCT device_name_raw, ' | ' LIMIT 3) AS device_names
  FROM g WHERE LENGTH(model_key) >= 2
  GROUP BY mfr, model_key
  HAVING n_raw_upper > 1
),
pcs AS (
  SELECT mfr, model_key, COUNT(DISTINCT pc) AS n_product_codes
  FROM g, UNNEST(product_codes) AS pc
  GROUP BY mfr, model_key
)
SELECT k.mfr, k.model_key, k.raw_spellings, k.n_di, p.n_product_codes, k.device_names
FROM keys AS k LEFT JOIN pcs AS p USING (mfr, model_key)
ORDER BY FARM_FINGERPRINT(CONCAT(k.mfr, '|', k.model_key))
LIMIT 25
