-- Title: MAUDE UDIs that are not a GUDID primary DI
-- Check: For MAUDE rows with a UDI that has no exact match among GUDID primary DIs, classifies the
--   UDI's shape (whitespace, GS1 "(01)" prefix, too long/short, lower case, unexpected characters,
--   or well-formed) and counts how many would match after a light normalization.
--   Same logic as _SHAPE_SQL in Data-Pipeline/scripts/analysis/bias_slices.py (length 6-23).
-- Why: These rows carry a UDI but cannot become UDI-derived labels. The bias report found 99.96% of
--   them are well-formed DIs that are simply not primary DIs (likely package or secondary DIs,
--   which GUDID prod does not keep).
-- Informs: Edge case "UDI not found as a primary DI": not a training label; treat as no-UDI.
WITH gudid_di AS (SELECT DISTINCT udi_di FROM `__GUDID__` WHERE udi_di IS NOT NULL),
gudid_upper AS (SELECT DISTINCT UPPER(udi_di) AS k FROM gudid_di),
unmatched AS (
  SELECT t.udi_di FROM `__MAUDE__` AS t
  LEFT JOIN gudid_di AS g ON t.udi_di = g.udi_di
  WHERE t.udi_di IS NOT NULL AND g.udi_di IS NULL
),
shaped AS (
  SELECT
    CASE
      WHEN udi_di != TRIM(udi_di)                 THEN 'whitespace'
      WHEN STARTS_WITH(udi_di, '(01)')            THEN 'gs1_prefix_(01)'
      WHEN REGEXP_CONTAINS(udi_di, r'^01\d{14}')  THEN 'gs1_prefix_01_no_parens'
      WHEN LENGTH(udi_di) > 23                    THEN 'longer_than_23'
      WHEN LENGTH(udi_di) < 6                     THEN 'shorter_than_6'
      WHEN udi_di != UPPER(udi_di)                THEN 'lower_case'
      WHEN REGEXP_CONTAINS(udi_di, r'[^0-9A-Z+$/.\-]') THEN 'unexpected_chars'
      ELSE 'well_formed_not_in_gudid'
    END AS shape,
    COALESCE(REGEXP_EXTRACT(udi_di, r'\(01\)\s*(\d{14})'),
             REGEXP_EXTRACT(udi_di, r'^01(\d{14})'),
             UPPER(REGEXP_REPLACE(udi_di, r'\s', ''))) AS normalized
  FROM unmatched
)
SELECT shape, COUNT(*) AS n_unmatched, COUNTIF(u.k IS NOT NULL) AS n_would_match_normalized
FROM shaped LEFT JOIN gudid_upper AS u ON shaped.normalized = u.k
GROUP BY shape
ORDER BY n_unmatched DESC
