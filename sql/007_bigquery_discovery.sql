-- BigQuery discovery landing table + latest view.
-- Applied by scripts/08_bigquery.sh sentinel_discovery (substitutes __PROJECT__).
--
-- Same append-only-plus-view pattern as sentinel_raw.incidents_v2.
-- A discovery run at 09:00 and another at 09:30 both accumulate; the view
-- gives the current set (one row per incident_id).

CREATE TABLE IF NOT EXISTS `__PROJECT__.sentinel_raw.discovered_ids` (
  incident_id   STRING NOT NULL,
  discovered_at TIMESTAMP,
  filter_hash   STRING NOT NULL,
  cursor_page   INT64,
  partial       BOOL,
  cursor_pages_fetched INT64,
  cursor_page_cap INT64,
  _request_id   STRING NOT NULL,
  _page_no      INT64 NOT NULL,
  _raw_uri      STRING NOT NULL,
  _ingested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(_ingested_at)
CLUSTER BY incident_id, filter_hash
OPTIONS (
  description = "Append-only discovery id landing — which incidents a filter returned and when"
);

CREATE OR REPLACE VIEW `__PROJECT__.sentinel_core.discovered_ids_latest` AS
SELECT * EXCEPT (rn)
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY incident_id
      ORDER BY _ingested_at DESC
    ) AS rn
  FROM `__PROJECT__.sentinel_raw.discovered_ids`
)
WHERE rn = 1;
