-- Title: Placeholder values in model and catalog numbers
-- Check: The 75 most frequent MAUDE model-number and catalog-number values (upper-cased, trimmed),
--   each with its count, share of non-null values, and whether it matches the draft placeholder
--   rule (UNK, UNKNOWN, N/A, NI, NONE, ASKU, NAV, NOT APPLICABLE, runs of 0 / * / ? / X, or under
--   2 characters after removing spaces and - _ . /).
-- Why: A model number like "UNK" or "N/A" is not evidence. If placeholders are counted as real
--   values, "UNK" = "UNK" looks like an exact match and produces false merges.
-- Informs: The list of values the definition treats as missing (edge case: placeholder model number).
--   Rows near the top with is_draft_placeholder = false are candidates to add to the list.
WITH vals AS (
  SELECT 'model_number' AS field, UPPER(TRIM(model_number_raw)) AS v
  FROM `__MAUDE__` WHERE NULLIF(TRIM(model_number_raw), '') IS NOT NULL
  UNION ALL
  SELECT 'catalog_number', UPPER(TRIM(catalog_number))
  FROM `__MAUDE__` WHERE NULLIF(TRIM(catalog_number), '') IS NOT NULL
),
counted AS (
  SELECT field, v AS value, COUNT(*) AS n
  FROM vals GROUP BY field, v
)
SELECT
  field,
  value,
  n,
  ROUND(n / SUM(n) OVER (PARTITION BY field), 6) AS share_of_nonnull,
  (LENGTH(REGEXP_REPLACE(value, r'[\s\-_./]', '')) < 2
   OR REGEXP_CONTAINS(REGEXP_REPLACE(value, r'[\s\-_./]', ''),
        r'^(UNK|UNKN|UNKNOWN|NA|NI|NONE|NAV|ASKU|NOTAPPLICABLE|NOTAVAILABLE|NOTKNOWN|UNAVAILABLE|NOTPROVIDED|0+|\*+|\?+|X+)$')
  ) AS is_draft_placeholder
FROM counted
QUALIFY ROW_NUMBER() OVER (PARTITION BY field ORDER BY n DESC, value) <= 75
ORDER BY field, n DESC
