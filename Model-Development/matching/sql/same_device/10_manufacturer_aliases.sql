-- Title: Manufacturer name aliases on UDI-linked rows
-- Check: On MAUDE rows linked to GUDID by exact primary DI, counts how often the normalized
--   manufacturer names differ, and lists the 50 most frequent (MAUDE name, GUDID name) pairs that
--   differ, with how many distinct DIs each covers. Part `summary` gives the overall share.
-- Why: MAUDE often names a subsidiary, distributor or pre-acquisition company while GUDID names
--   the labeler. If the definition demands an exact manufacturer match, those true matches become NON_MATCH,
--   and the "big vs small manufacturer" slice will look worse than it is.
-- Informs: Edge case "manufacturer alias / acquisition": MATCH when model and product code agree;
--   seeds an alias table for the shared feature function.
-- @part summary
WITH g AS (
  SELECT udi_di, manufacturer_name_norm AS g_mfr
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY udi_di ORDER BY publish_date DESC, source_record_id) = 1
)
SELECT
  COUNT(*) AS n_linked_rows,
  COUNTIF(m.manufacturer_name_norm = g.g_mfr) AS n_same_mfr,
  COUNTIF(m.manufacturer_name_norm != g.g_mfr) AS n_diff_mfr,
  COUNTIF(m.manufacturer_name_norm IS NULL OR g.g_mfr IS NULL) AS n_mfr_missing,
  ROUND(COUNTIF(m.manufacturer_name_norm != g.g_mfr) / COUNT(*), 6) AS pct_diff_mfr,
  COUNT(DISTINCT IF(m.manufacturer_name_norm != g.g_mfr,
                    CONCAT(m.manufacturer_name_norm, '|', g.g_mfr), NULL)) AS n_distinct_alias_pairs
FROM `__MAUDE__` AS m
JOIN g USING (udi_di)
-- @part top_pairs
WITH g AS (
  SELECT udi_di, manufacturer_name_norm AS g_mfr
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY udi_di ORDER BY publish_date DESC, source_record_id) = 1
)
SELECT
  m.manufacturer_name_norm AS maude_mfr,
  g.g_mfr AS gudid_mfr,
  COUNT(*) AS n_rows,
  COUNT(DISTINCT udi_di) AS n_dis
FROM `__MAUDE__` AS m
JOIN g USING (udi_di)
WHERE m.manufacturer_name_norm != g.g_mfr
GROUP BY maude_mfr, gudid_mfr
ORDER BY n_rows DESC, maude_mfr, gudid_mfr
LIMIT 50
