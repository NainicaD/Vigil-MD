-- Title: GUDID labelers for the embedding prototype (big, medium and small)
-- Check: Ranks GUDID labelers (manufacturer_name_norm on the GUDID side) by how many MAUDE rows
--   link to their primary DIs by exact UDI, puts them in tiers (top 10, 11-100, 101+), and draws
--   @n_top, @n_mid and @n_small labelers per tier in a fixed pseudo-random order (@seed). Labelers
--   with more than @max_dis_per_labeler DIs, or fewer than @min_linked_rows linked MAUDE rows,
--   are left out.
-- Why: Embedding all 5.26M GUDID DIs is too slow for a laptop. A slice of labelers keeps every
--   device of each chosen labeler, so the search still has to tell a device from its siblings
--   (sizes, suffix variants). Labelers are picked on the GUDID side, so MAUDE rows filed under
--   another name of the same labeler (aliases, E5) are still in the evaluation.
-- Informs: Which labelers the prototype covers. The tiers are a bias slice. Note that this tier
--   ranks GUDID labelers by linked MAUDE rows, not MAUDE manufacturers by rows as query 14 does.
WITH gudid_di AS (
  SELECT udi_di, manufacturer_name_norm AS mfr
  FROM `__GUDID__`
  WHERE udi_di IS NOT NULL AND manufacturer_name_norm IS NOT NULL
),
linked AS (
  SELECT d.mfr, COUNT(*) AS n_linked_rows
  FROM `__MAUDE__` AS t
  JOIN gudid_di AS d ON t.udi_di = d.udi_di
  GROUP BY d.mfr
),
gudid_size AS (
  SELECT manufacturer_name_norm AS mfr, COUNT(*) AS n_dis
  FROM `__GUDID__`
  WHERE manufacturer_name_norm IS NOT NULL
  GROUP BY mfr
),
ranked AS (
  SELECT l.mfr, l.n_linked_rows, s.n_dis,
    ROW_NUMBER() OVER (ORDER BY l.n_linked_rows DESC, l.mfr) AS link_rank
  FROM linked AS l
  JOIN gudid_size AS s USING (mfr)
),
tiered AS (
  SELECT *,
    CASE WHEN link_rank <= 10 THEN 'top_10'
         WHEN link_rank <= 100 THEN '11_100'
         ELSE '101_plus' END AS tier
  FROM ranked
  WHERE n_dis <= @max_dis_per_labeler AND n_linked_rows >= @min_linked_rows
),
drawn AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY tier ORDER BY FARM_FINGERPRINT(CONCAT(@seed, '|', mfr))) AS draw
  FROM tiered
)
SELECT mfr, tier, link_rank, n_linked_rows, n_dis
FROM drawn
WHERE (tier = 'top_10' AND draw <= @n_top)
   OR (tier = '11_100' AND draw <= @n_mid)
   OR (tier = '101_plus' AND draw <= @n_small)
ORDER BY link_rank
