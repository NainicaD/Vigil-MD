-- Title: Device split: one train/test assignment per GUDID model group (N2)
-- Check: Part `device_split` gives every GUDID base_key (the device_key macro: GUDID labeler | model key, or
--   catalog key when the model is unusable) a stemmed device_key and a split. Stem merge: within one
--   labeler, a key that equals a shorter key of that labeler plus 1-3 trailing characters
--   (the suffix_lens macro, E3) takes the shortest such key; this is applied at most twice. split is
--   the split macro on the stemmed key (SHA-256 bucket, the same as split_of in Python).
--   Part `device_split_summary` is one row of diagnostics: key counts, the largest groups, chains cut
--   at the second level, and the test share.
-- Why: The split unit is the model group, not the DI (13.5% of DIs share a labeler and model, so a DI
--   split puts siblings on both sides) and not the labeler (the top 10 are 46% of MAUDE rows). Suffix
--   siblings such as GP200 / GP200S are exactly the hard negatives, so they must be on the same side;
--   otherwise a test negative was seen in training. GUDID's own labeler name is used, not an alias, so
--   the split exists before the train-only alias table (E5). The merge stops after two levels so a
--   labeler cannot collapse into one huge group.
-- Informs: 01_alias_table.sql (train devices only), 02_positives.sql (columns base_key, device_key,
--   split), validate_labels.py C2 and C6. One row per base_key; every GUDID base_key is covered.
-- Placeholders filled by build_labels.py: __MODEL_DATASET__. No regex literals: rules are macros.
-- @part device_split
CREATE OR REPLACE TABLE `__MODEL_DATASET__.device_split` AS
WITH g AS (
  SELECT IFNULL(manufacturer_name_norm, '') AS mfr,
         {{ device_key(manufacturer_name_norm, model_number_raw, catalog_number) }} AS base_key,
         udi_di
  FROM `__GUDID__`
),
keys AS (
  -- The key part of base_key, cut after "labeler|" (device_key writes IFNULL(labeler, '') + '|').
  SELECT mfr, base_key, SUBSTR(base_key, LENGTH(mfr) + 2) AS k, COUNT(DISTINCT udi_di) AS n_dis
  FROM g
  WHERE base_key IS NOT NULL
  GROUP BY mfr, base_key
),
parent AS (
  -- The shortest key of the same labeler that this key extends by 1-3 characters (query 07's join).
  SELECT c.base_key,
         ARRAY_AGG(p.base_key ORDER BY LENGTH(p.k), p.k LIMIT 1)[OFFSET(0)] AS parent_key
  FROM keys AS c, UNNEST({{ suffix_lens() }}) AS d
  JOIN keys AS p
    ON p.mfr = c.mfr AND p.k = SUBSTR(c.k, 1, LENGTH(c.k) - d)
  WHERE LENGTH(c.k) > d
  GROUP BY c.base_key
),
stemmed AS (
  SELECT
    k.base_key,
    k.n_dis,
    COALESCE(p2.parent_key, p1.parent_key, k.base_key) AS device_key,
    CASE WHEN p2.parent_key IS NOT NULL THEN 2
         WHEN p1.parent_key IS NOT NULL THEN 1
         ELSE 0 END AS stem_levels,
    -- The chain went on past two levels: this key and its stem's own parent end up apart.
    p3.parent_key IS NOT NULL AS stem_truncated
  FROM keys AS k
  LEFT JOIN parent AS p1 ON p1.base_key = k.base_key
  LEFT JOIN parent AS p2 ON p2.base_key = p1.parent_key
  LEFT JOIN parent AS p3 ON p3.base_key = p2.parent_key
)
SELECT
  base_key,
  device_key,
  {{ split(device_key) }} AS split,
  stem_levels,
  stem_truncated,
  n_dis,
  COUNT(*) OVER (PARTITION BY device_key) AS n_base_keys_in_device,
  SUM(n_dis) OVER (PARTITION BY device_key) AS n_dis_in_device
FROM stemmed
-- @part device_split_summary
CREATE OR REPLACE TABLE `__MODEL_DATASET__.device_split_summary` AS
WITH dev AS (
  SELECT device_key, ANY_VALUE(split) AS split, COUNT(*) AS n_base_keys, SUM(n_dis) AS n_dis
  FROM `__MODEL_DATASET__.device_split`
  GROUP BY device_key
)
SELECT
  (SELECT COUNT(*) FROM `__MODEL_DATASET__.device_split`) AS n_base_keys,
  (SELECT COUNT(DISTINCT base_key) FROM `__MODEL_DATASET__.device_split`) AS n_distinct_base_keys,
  (SELECT COUNT(*) FROM dev) AS n_device_keys,
  (SELECT COUNTIF(stem_levels > 0) FROM `__MODEL_DATASET__.device_split`) AS n_base_keys_merged,
  (SELECT COUNTIF(stem_truncated) FROM `__MODEL_DATASET__.device_split`) AS n_base_keys_truncated,
  (SELECT MAX(n_base_keys) FROM dev) AS max_base_keys_per_device,
  (SELECT MAX(n_dis) FROM dev) AS max_dis_per_device,
  (SELECT ARRAY_AGG(STRUCT(device_key, n_base_keys, n_dis) ORDER BY n_base_keys DESC, device_key LIMIT 20)
   FROM dev) AS largest_devices,
  (SELECT COUNTIF(split = 'test') / COUNT(*) FROM dev) AS test_share_devices,
  (SELECT SUM(IF(split = 'test', n_dis, 0)) / SUM(n_dis) FROM dev) AS test_share_dis,
  (SELECT COUNT(*) FROM dev WHERE split NOT IN ('train', 'test') OR split IS NULL) AS n_bad_split
