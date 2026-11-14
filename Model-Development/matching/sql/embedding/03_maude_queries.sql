-- Title: UDI-linked MAUDE tuples whose device belongs to the chosen labelers (known answers)
-- Check: MAUDE rows whose udi_di equals the primary DI of a GUDID record of @labelers (the exact
--   rule of exact_linking.py). Rows are collapsed to distinct (manufacturer, model key, catalog key,
--   device name) tuples, as in the test set; each tuple keeps every GUDID group its rows link to.
--   At most @max_queries tuples are kept, in a fixed pseudo-random order (@seed). Flags per tuple:
--   id_agrees (a MAUDE identifier equals a GUDID identifier of the linked group, any field),
--   mfr_equal (the normalized manufacturer names are equal), has_identifier.
-- Why: The UDI link is the answer, so retrieval can be scored without hand labels. The udi_di
--   itself is never returned, so it cannot leak into the text that is embedded. Tuples whose
--   identifiers disagree with the link (E1 conflicts) are kept but flagged, so recall can be
--   reported with and without them.
-- Informs: The query side of the embedding evaluation and its slices.
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
linked AS (
  SELECT
    t.source_record_id AS mdr_report_key,
    t.device_seq,
    t.manufacturer_name_norm AS mfr,
    t.manufacturer_name_raw, t.device_name_raw, t.model_number_raw, t.catalog_number,
    t.product_code, t.record_date,
    g2.group_key, g2.mfr AS gudid_mfr, g2.mk AS g_mk, g2.ck AS g_ck,
    UPPER(REGEXP_REPLACE(TRIM(t.model_number_raw), r'[\s\-_./]', '')) AS mk_raw,
    UPPER(REGEXP_REPLACE(TRIM(t.catalog_number), r'[\s\-_./]', '')) AS ck_raw
  FROM `__MAUDE__` AS t
  JOIN g2 ON t.udi_di = g2.udi_di
),
keyed AS (
  SELECT
    l.* EXCEPT (mk_raw, ck_raw),
    IF(l.mk_raw IS NOT NULL AND LENGTH(l.mk_raw) >= 2 AND NOT REGEXP_CONTAINS(l.mk_raw,
         r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'),
       l.mk_raw, NULL) AS model_key,
    IF(l.ck_raw IS NOT NULL AND LENGTH(l.ck_raw) >= 2 AND NOT REGEXP_CONTAINS(l.ck_raw,
         r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$'),
       l.ck_raw, NULL) AS catalog_key,
    NULLIF(UPPER(TRIM(l.device_name_raw)), '') AS name_key
  FROM linked AS l
),
tuples AS (
  SELECT
    mfr, model_key, catalog_key, name_key,
    COUNT(*) AS n_rows,
    ARRAY_AGG(DISTINCT group_key ORDER BY group_key) AS true_group_keys,
    ARRAY_AGG(DISTINCT gudid_mfr IGNORE NULLS ORDER BY gudid_mfr) AS gudid_mfrs,
    LOGICAL_OR(model_key IN (g_mk, g_ck) OR catalog_key IN (g_mk, g_ck)) AS id_agrees,
    LOGICAL_OR(mfr = gudid_mfr) AS mfr_equal,
    ARRAY_AGG(STRUCT(mdr_report_key, device_seq, manufacturer_name_raw, device_name_raw,
                     model_number_raw, catalog_number, product_code, record_date)
              ORDER BY FARM_FINGERPRINT(CONCAT(@seed, '|', IFNULL(mdr_report_key, ''), '|',
                                               CAST(device_seq AS STRING)))
              LIMIT 1)[OFFSET(0)] AS ex
  FROM keyed
  WHERE COALESCE(mfr, model_key, catalog_key, name_key) IS NOT NULL
  GROUP BY mfr, model_key, catalog_key, name_key
),
drawn AS (
  SELECT *,
    COUNT(*) OVER () AS pool_tuples,
    ROW_NUMBER() OVER (ORDER BY FARM_FINGERPRINT(CONCAT(@seed, '|', IFNULL(mfr, ''), '|',
                       IFNULL(model_key, ''), '|', IFNULL(catalog_key, ''), '|',
                       IFNULL(name_key, '')))) AS draw
  FROM tuples
)
SELECT
  SUBSTR(TO_HEX(MD5(CONCAT(IFNULL(mfr, ''), '|', IFNULL(model_key, ''), '|',
                           IFNULL(catalog_key, ''), '|', IFNULL(name_key, '')))), 1, 12)
    AS query_id,
  pool_tuples,
  n_rows,
  ARRAY_TO_STRING(true_group_keys, ';') AS true_group_keys,
  ARRAY_TO_STRING(gudid_mfrs, ';') AS gudid_mfrs,
  IFNULL(id_agrees, FALSE) AS id_agrees,
  IFNULL(mfr_equal, FALSE) AS mfr_equal,
  (model_key IS NOT NULL OR catalog_key IS NOT NULL) AS has_identifier,
  mfr,
  ex.manufacturer_name_raw,
  ex.device_name_raw,
  ex.model_number_raw,
  ex.catalog_number,
  ex.product_code,
  ex.record_date,
  ex.mdr_report_key AS example_mdr_report_key,
  ex.device_seq AS example_device_seq
FROM drawn
WHERE draw <= @max_queries
ORDER BY query_id
