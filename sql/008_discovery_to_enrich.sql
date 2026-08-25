-- Bridge: discovered but not yet enriched.
-- Run by scripts/08_bigquery.sh sentinel_discovery (substitutes __PROJECT__).
--
-- THIS QUERY IS THE SEAM BETWEEN DISCOVERY AND ENRICHMENT, and it deliberately
-- lives in SQL rather than in the collector. Deciding what still needs collecting
-- is a business decision — it depends on staleness rules that differ per issue
-- type — and it belongs with LiSN, not in the fetch layer. The collector stays
-- dumb about what should be collected; it collects exactly what it is told.
--
-- Optional restrictions (uncomment / bind as needed):
--   AND d.filter_hash = '…'
--   AND d.discovered_at >= TIMESTAMP('…')
--   AND d.discovered_at <  TIMESTAMP('…')

SELECT
  d.incident_id,
  d.filter_hash,
  d.discovered_at,
  d._ingested_at AS discovered_ingested_at
FROM `__PROJECT__.sentinel_core.discovered_ids_latest` AS d
LEFT JOIN `__PROJECT__.sentinel_core.incidents_current` AS i
  ON i.id = d.incident_id
WHERE i.id IS NULL
ORDER BY d.incident_id;
