-- Migrate orderItemId / orderItemUnitId / threads_communicationId off FLOAT64.
--
-- Numbering note: requested as sql/009_migrate_id_columns.sql, but 009–012 are
-- already taken (shortfall / discovery_window / discovery_gaps / priority).
--
-- WHY THIS IS THE INTERESTING PART:
-- We are recovering from a field-mapping mistake by re-parsing raw objects we
-- already stored in the GCS raw zone, NOT by re-querying the source. That is
-- exactly what the append-only raw zone was built for — evidence of what the
-- source returned, durable enough to rebuild a corrected landing table when the
-- mapping was wrong. scripts/36_backfill_ids.py is that recovery. It has now
-- been used in anger.
--
-- BigQuery cannot ALTER FLOAT64 → STRING. The old sentinel_raw.incidents table
-- keeps its corrupted FLOAT64 values; we do NOT copy from it — casting a
-- rounded float back to STRING preserves the rounding. Backfill comes from GCS.
--
-- Greenfield installs: sql/003_bigquery.sql now creates incidents_v2 and the
-- serving view. This file remains for the GCS backfill recovery path
-- (scripts/36_backfill_ids.py) on instances that still have the old table.
--
-- This file only CREATES incidents_v2. It does not switch the serving view —
-- that happens at swap time in scripts/36_backfill_ids.py --swap, after
-- reconcile, so a half-filled v2 never becomes the live table.
--
-- Apply with:
--   sed "s/__PROJECT__/${PROJECT}/g" sql/013_migrate_id_columns.sql | bq query ...

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
  description = "Append-only Sentinel raw landing (STRING identifiers) — rebuilt from GCS raw via scripts/36_backfill_ids.py; never copied from FLOAT64 incidents"
);
