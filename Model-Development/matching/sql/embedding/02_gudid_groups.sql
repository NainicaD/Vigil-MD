-- Title: GUDID model groups of the chosen labelers (the index the embeddings search)
-- Check: Every GUDID record of the labelers in @labelers, grouped at the model level of
--   SAME_DEVICE_DEFINITION.md section 1: one group per (labeler, model key); a record with no
--   usable model key is its own group. Each group is described by its newest DI, plus up to five
--   catalog numbers.
-- Why: These groups are what retrieval returns. The group_key is built exactly as in
--   sql/test_set/02_candidates.sql, so results can be joined to the labeling sheet.
-- Informs: The text that is embedded for the GUDID side.
WITH
-- BEGIN gudid_group_keys (keep identical in 02 and 03: the group_key must match)
g_raw AS (
  SELECT
    udi_di, source_record_id, publish_date,
    manufacturer_name_norm AS mfr,
    manufacturer_name_raw, device_name_raw, model_number_raw, catalog_number, product_codes,
    UPPER(REGEXP_REPLACE(TRIM(model_number_raw), r'[\s\-_./]', '')) AS mk_raw,
    UPPER(REGEXP_REPLACE(TRIM(catalog_number), r'[\s\-_./]', '')) AS ck_raw
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IN UNNEST(@labelers)
),
g AS (
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
-- END gudid_group_keys
model_groups AS (
  SELECT
    group_key,
    COUNT(*) AS n_dis,
    ARRAY_AGG(STRUCT(udi_di, mfr, manufacturer_name_raw, device_name_raw, model_number_raw,
                     catalog_number, product_codes)
              ORDER BY publish_date DESC, source_record_id LIMIT 1)[OFFSET(0)] AS rep,
    ARRAY_AGG(DISTINCT catalog_number IGNORE NULLS ORDER BY catalog_number LIMIT 5)
      AS catalog_examples
  FROM g2
  GROUP BY group_key
)
SELECT
  group_key,
  n_dis,
  rep.udi_di AS rep_udi_di,
  rep.mfr AS mfr,
  rep.manufacturer_name_raw,
  rep.device_name_raw,
  rep.model_number_raw,
  rep.catalog_number,
  ARRAY_TO_STRING(catalog_examples, ';') AS catalog_examples,
  ARRAY_TO_STRING(rep.product_codes, ';') AS product_codes
FROM model_groups
ORDER BY group_key
