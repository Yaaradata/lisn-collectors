-- Shortfall columns on collector_job (idempotent for already-deployed DBs).
-- Fresh installs get these from sql/001_collector.sql.

ALTER TABLE collector_job
  ADD COLUMN IF NOT EXISTS requested_count integer;

ALTER TABLE collector_job
  ADD COLUMN IF NOT EXISTS returned_count integer;

ALTER TABLE collector_job
  ADD COLUMN IF NOT EXISTS missing_keys jsonb;

COMMENT ON COLUMN collector_job.requested_count IS
  'Keys we asked for (page payload length).';

COMMENT ON COLUMN collector_job.returned_count IS
  'Distinct source entities that came back (not thread-exploded rows).';

COMMENT ON COLUMN collector_job.record_count IS
  'Rows written to BigQuery; higher than returned_count under thread explosion.';

COMMENT ON COLUMN collector_job.missing_keys IS
  'Sample of missing/unexpected keys vs the page payload; anomaly, not always error.';
