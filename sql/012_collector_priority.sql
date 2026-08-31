-- Priority on collector_job (idempotent for already-deployed DBs).
-- Mirrors procrastinate_jobs.priority so requeues and debugging see the same value.

ALTER TABLE collector_job
  ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'collector_job_priority_range'
  ) THEN
    ALTER TABLE collector_job
      ADD CONSTRAINT collector_job_priority_range
      CHECK (priority >= 0 AND priority <= 10);
  END IF;
END $$;

COMMENT ON COLUMN collector_job.priority IS
  'Queue order only (Procrastinate priority). Does not bypass rate limits or preempt in-flight work.';
