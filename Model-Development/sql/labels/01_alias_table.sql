-- Title: Train-only labeler alias table (N3)
-- Check: Part `alias_table` lists (maude_mfr, gudid_mfr) pairs of normalized labeler names that are
--   linked by an exact UDI (MAUDE udi_di = GUDID primary DI, the rule of exact_linking.py) on devices
--   of the TRAIN split only, where the names differ, with at least ALIAS_MIN_DIS distinct DIs (the
--   alias_min_dis macro). The build stops with an error if the table would be empty. Part
--   `alias_split_diagnostic` counts, for each pair whose MAUDE name is also a GUDID labeler, the keys
--   both labelers have and how many of them sit in different splits.
-- Why: 43.8% of UDI-linked rows have different labeler names (E5), so exact name equality cannot be
--   required. Built from train devices only, so test devices never leak through it. The DI minimum
--   keeps one-off mis-entries out. Pairs with equal names are left out: they add nothing
--   (feat_labeler_equal covers them). The diagnostic sizes the accepted residual leak: GUDID's own
--   aliases (zimmer dental / zimvie us corp) can put one physical device in both splits under two
--   names, which check C2 cannot see.
-- Informs: 04_negatives_same_labeler.sql (alias-expanded labelers), feat_labeler_alias_equal.
--   build_labels.py --export-alias writes labels/alias_table_v1.csv and its hash; steps 02+ refuse to
--   run unless BigQuery still matches that hash.
-- Placeholders filled by build_labels.py: __MODEL_DATASET__. No regex literals: rules are macros.
-- @part alias_table
CREATE OR REPLACE TABLE `__MODEL_DATASET__.alias_table` AS
WITH g AS (
  SELECT udi_di, manufacturer_name_norm AS gudid_mfr,
         {{ device_key(manufacturer_name_norm, model_number_raw, catalog_number) }} AS base_key
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL AND manufacturer_name_norm IS NOT NULL
),
train_di AS (
  SELECT g.udi_di, g.gudid_mfr
  FROM g
  JOIN `__MODEL_DATASET__.device_split` AS s USING (base_key)
  WHERE s.split = 'train'
),
pairs AS (
  SELECT m.manufacturer_name_norm AS maude_mfr, t.gudid_mfr,
         COUNT(DISTINCT t.udi_di) AS n_dis, COUNT(*) AS n_rows
  FROM `__MAUDE__` AS m
  JOIN train_di AS t ON m.udi_di = t.udi_di
  WHERE m.manufacturer_name_norm IS NOT NULL AND m.manufacturer_name_norm != t.gudid_mfr
  GROUP BY maude_mfr, gudid_mfr
  HAVING COUNT(DISTINCT t.udi_di) >= {{ alias_min_dis() }}
)
SELECT * FROM pairs
WHERE IF((SELECT COUNT(*) FROM pairs) = 0, ERROR('alias_table would be empty'), TRUE)
-- @part alias_split_diagnostic
CREATE OR REPLACE TABLE `__MODEL_DATASET__.alias_split_diagnostic` AS
WITH k AS (
  SELECT DISTINCT IFNULL(manufacturer_name_norm, '') AS mfr,
         {{ device_key(manufacturer_name_norm, model_number_raw, catalog_number) }} AS base_key
  FROM `__GUDID__`
),
kk AS (
  SELECT k.mfr, SUBSTR(k.base_key, LENGTH(k.mfr) + 2) AS key_part, s.split
  FROM k
  JOIN `__MODEL_DATASET__.device_split` AS s USING (base_key)
)
SELECT a.maude_mfr, a.gudid_mfr, a.n_dis,
       COUNT(*) AS n_shared_keys,
       COUNTIF(x.split != y.split) AS n_shared_keys_split_apart
FROM `__MODEL_DATASET__.alias_table` AS a
JOIN kk AS x ON x.mfr = a.maude_mfr
JOIN kk AS y ON y.mfr = a.gudid_mfr AND y.key_part = x.key_part
GROUP BY a.maude_mfr, a.gudid_mfr, a.n_dis
