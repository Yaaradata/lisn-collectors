-- Discovery window ledger: which calendar windows this collector has processed.
--
-- DESIGN DISTINCTION (do not reopen lightly):
-- LiSN owns WHAT to collect — completeness rules differ per issue type and
-- belong with the business logic. That decision stands. DETECTING a gap is
-- not the same as DECIDING what to collect, and only the collector knows
-- which windows it has actually processed. Detection belongs HERE.
-- Scheduling (whether / when to fill a gap) stays with LiSN.
--
-- window_field matters: a window over updated_on and a window over created_at
-- are different timelines and must not be compared for contiguity with each
-- other (see sql/011_discovery_gaps.sql).
--
-- Apply after sql/001_collector.sql.

CREATE TABLE IF NOT EXISTS discovery_window (
  window_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source        text NOT NULL,
  request_id    uuid NOT NULL REFERENCES collector_request(request_id) ON DELETE CASCADE,
  -- 'updated_on' or 'created_at' — which time axis the window covers.
  window_field  text NOT NULL,
  window_from   timestamptz NOT NULL,
  window_to     timestamptz NOT NULL,
  id_count      integer,
  status        text NOT NULL DEFAULT 'running',
  -- Submit carried allow_gap=true — gap was intentional (still surfaces in
  -- /v1/discovery/gaps — we do not auto-backfill). Kept for the gaps half.
  allow_gap     boolean NOT NULL DEFAULT false,
  gap_reason    text,
  started_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz,
  CHECK (window_from < window_to),
  CHECK (status IN ('running', 'complete', 'partial', 'failed')),
  CHECK (window_field IN ('updated_on', 'created_at'))
);

COMMENT ON TABLE discovery_window IS
  'Ledger of discovery windows this collector has processed. '
  'DETECTING a coverage gap belongs here (only the collector knows which '
  'windows actually ran). DECIDING what to collect / whether to backfill '
  'stays with LiSN — completeness rules differ per issue type. Do not move '
  'gap detection into LiSN or auto-backfill from this table.';

CREATE INDEX IF NOT EXISTS idx_discovery_window_coverage
  ON discovery_window (source, window_field, window_from);

CREATE INDEX IF NOT EXISTS idx_discovery_window_request
  ON discovery_window (request_id);

-- Upgrade path from earlier 010 drafts (updated/created, created_at column,
-- window_to >= window_from, missing id_count). Drop CHECKs first so the
-- window_field rename is not blocked by a legacy field-token constraint.
DO $upgrade$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'discovery_window'::regclass
      AND contype = 'c'
  LOOP
    EXECUTE format('ALTER TABLE discovery_window DROP CONSTRAINT %I', r.conname);
  END LOOP;

  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'discovery_window' AND column_name = 'created_at'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'discovery_window' AND column_name = 'started_at'
  ) THEN
    ALTER TABLE discovery_window RENAME COLUMN created_at TO started_at;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'discovery_window' AND column_name = 'id_count'
  ) THEN
    ALTER TABLE discovery_window ADD COLUMN id_count integer;
  END IF;

  UPDATE discovery_window SET window_field = 'updated_on' WHERE window_field = 'updated';
  UPDATE discovery_window SET window_field = 'created_at' WHERE window_field = 'created';

  ALTER TABLE discovery_window
    ADD CONSTRAINT discovery_window_from_before_to CHECK (window_from < window_to),
    ADD CONSTRAINT discovery_window_status_check
      CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    ADD CONSTRAINT discovery_window_field_check
      CHECK (window_field IN ('updated_on', 'created_at'));
END
$upgrade$;
