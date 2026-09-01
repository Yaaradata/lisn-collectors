-- BigQuery landing table + current view for Sentinel enrichment.
-- Applied by scripts/08_bigquery.sh (substitutes __PROJECT__).
--
-- Append-only. Fetch the same incident three times and there are three rows.
-- This is the evidence store — proof of what a query returned and when.
--
-- Landing table is incidents_v2, not incidents. The original sentinel_raw.incidents
-- used FLOAT64 for orderItemId, orderItemUnitId and threads_communicationId, which
-- silently mutated identifiers above 2^53. BigQuery cannot ALTER FLOAT64 → STRING,
-- so recovery required a new table rebuilt from the GCS raw zone (see
-- sql/013_migrate_id_columns.sql and scripts/36_backfill_ids.py). The old table
-- has been dropped; v2 is the permanent landing table until a deliberate rename.

CREATE TABLE IF NOT EXISTS `__PROJECT__.sentinel_raw.incidents_v2` (
  id STRING NOT NULL,
  issue_id INT64,
  issue_name STRING,
  issue_parentResponse_id INT64,
  issue_parentResponse_name STRING,
  orderId STRING,
  orderItemId STRING,
  orderItemUnitId STRING,
  trackingId STRING,
  orderItemProductFSN STRING,
  incidentScore INT64,
  resolutionDeadline TIMESTAMP,
  resolutionDeadlineBreach BOOL,
  sellerId STRING,
  source STRING,
  status_id INT64,
  status_status STRING,
  status_statusType STRING,
  subject STRING,
  updatedOn TIMESTAMP,
  agingScore INT64,
  lastUpdatedByUser STRING,
  queue STRING,
  assignedTo STRING,
  threads_id STRING,
  threads_channel_id INT64,
  threads_channel_name STRING,
  threads_communicationId STRING,
  threads_contentType STRING,
  threads_createdAt TIMESTAMP,
  threads_createdBy STRING,
  threads_systemThread BOOL,
  threads_threadEntryType_id INT64,
  threads_threadEntryType_name STRING,
  threads_updatedBy STRING,
  _request_id STRING NOT NULL,
  _page_no INT64 NOT NULL,
  _raw_uri STRING NOT NULL,
  _ingested_at TIMESTAMP
)
PARTITION BY DATE(_ingested_at)
CLUSTER BY id, trackingId
OPTIONS (
  description = "Append-only Sentinel raw landing (STRING identifiers) — collector write target"
);

-- This view is where the merge happens. It replaces the per-page upsert we
-- deliberately did not put on the write path.
--
-- PARTITION BY id, threads_id — BOTH columns. The Sentinel export is
-- thread-exploded and our seed measured 2.481 threads per incident.
-- Partitioning on id alone would drop every thread but one, silently
-- discarding conversation history.
CREATE OR REPLACE VIEW `__PROJECT__.sentinel_core.incidents_current` AS
SELECT * EXCEPT (rn)
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY id, threads_id
      ORDER BY updatedOn DESC, _ingested_at DESC
    ) AS rn
  FROM `__PROJECT__.sentinel_raw.incidents_v2`
)
WHERE rn = 1;
