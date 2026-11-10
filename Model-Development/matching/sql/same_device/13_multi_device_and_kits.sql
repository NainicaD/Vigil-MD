-- Title: Multi-device reports, kits and systems
-- Check: Part `devices_per_report` counts MAUDE reports by how many devices they list (device_seq).
--   Part `kit_crosstab` checks UDI-linked rows: is the MAUDE device name and/or the GUDID device
--   name a kit, system, set, tray or pack (word match on the upper-cased name)?
-- Why: A report about one component of a system may carry the system's model number, or the
--   reverse. A kit DI and a component DI share the manufacturer and often the model stem, so they
--   look like matches. Multi-device reports also mean one MAUDE record can have several correct
--   GUDID links, one per device_seq.
-- Informs: Edge case "kit/system vs component": NON_MATCH unless both sides are the kit; confirms
--   the pair key must include device_seq, not just the report key.
-- @part devices_per_report
WITH per_report AS (
  SELECT source_record_id, COUNT(*) AS n_devices
  FROM `__MAUDE__`
  GROUP BY source_record_id
)
SELECT
  CASE WHEN n_devices >= 5 THEN '5+' ELSE CAST(n_devices AS STRING) END AS devices_in_report,
  COUNT(*) AS n_reports,
  SUM(n_devices) AS n_device_rows,
  ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 6) AS share_of_reports
FROM per_report
GROUP BY devices_in_report
ORDER BY devices_in_report
-- @part kit_crosstab
WITH g AS (
  SELECT udi_di, device_name_raw AS g_name
  FROM `__GUDID__`
  WHERE NULLIF(TRIM(udi_di), '') IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (PARTITION BY udi_di ORDER BY publish_date DESC, source_record_id) = 1
)
SELECT
  IFNULL(REGEXP_CONTAINS(UPPER(m.device_name_raw), r'\b(KIT|KITS|SYSTEM|SYSTEMS|SET|TRAY|PACK)\b'), FALSE) AS maude_is_kit,
  IFNULL(REGEXP_CONTAINS(UPPER(g.g_name), r'\b(KIT|KITS|SYSTEM|SYSTEMS|SET|TRAY|PACK)\b'), FALSE) AS gudid_is_kit,
  COUNT(*) AS n_rows,
  ROUND(COUNT(*) / SUM(COUNT(*)) OVER (), 6) AS share_of_linked
FROM `__MAUDE__` AS m
JOIN g USING (udi_di)
GROUP BY maude_is_kit, gudid_is_kit
ORDER BY n_rows DESC
