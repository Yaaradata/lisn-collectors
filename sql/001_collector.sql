-- Collector state schema for the shared `collector` database.
-- Apply BEFORE the Procrastinate schema. Targets: collector ONLY (not sentinel_mock).
--
-- LiSN (a separate system) sends a query naming a source. We page it, write one
-- row per page, and workers process those rows. These tables are shared by EVERY
-- collector (Sentinel, eKart, FDP, …) and distinguished by `source`, so LiSN
-- asking "how many jobs are open across everything?" is one SELECT.

-- ---------------------------------------------------------------------------
-- TABLE 1 — collector_request  (one row per LiSN request)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector_request (
  request_id   uuid PRIMARY KEY,
  source       text NOT NULL,
  query_spec   jsonb NOT NULL,
  total_pages  integer NOT NULL,
  status       text NOT NULL DEFAULT 'open',
  created_at   timestamptz NOT NULL DEFAULT now(),
  closed_at    timestamptz
);

-- ---------------------------------------------------------------------------
-- TABLE 2 — collector_job  (one row per PAGE — the unit of everything)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collector_job (
  job_id            uuid PRIMARY KEY,
  request_id        uuid NOT NULL REFERENCES collector_request(request_id),
  source            text NOT NULL,
  page_no           integer NOT NULL,
  page_payload      jsonb NOT NULL,
  status            text NOT NULL DEFAULT 'pending',
  attempts          integer NOT NULL DEFAULT 0,
  owner             text,
  -- A hard-killed worker leaves its row stuck at in_progress forever. The
  -- Sprint 4 sweeper resets rows whose lease expired. Nothing in Procrastinate
  -- does this for our table.
  lease_expires_at  timestamptz,
  raw_uri           text,
  -- raw_written_at and loaded_at are DELIBERATELY separate columns: the gap
  -- between them is what a reconcile query looks for. A page that landed in
  -- GCS but never reached BigQuery is otherwise a silent failure, and catching
  -- it was called non-negotiable in review.
  raw_written_at    timestamptz,
  loaded_at         timestamptz,
  -- Three counts — do not conflate:
  --   requested_count — keys we asked for (page payload length)
  --   returned_count  — distinct source entities that came back
  --   record_count    — rows written to BigQuery (higher under thread explosion)
  requested_count   integer,
  returned_count    integer,
  record_count      integer,
  -- Sample of keys in the shortfall / reverse shortfall (capped); null when
  -- the page has no key list (e.g. discovery). A shortfall is an ANOMALY, not
  -- necessarily an error — a key can legitimately not exist.
  missing_keys      jsonb,
  -- Queue order hint for Procrastinate (higher runs first). Default 0.
  -- Affects QUEUE ORDER only — not rate limits, not in-flight preemption.
  priority          integer NOT NULL DEFAULT 0,
  -- W3C Trace Context carrier (JSON). API injects under collect_request;
  -- workers extract so fetch_page is a child span across process boundaries.
  trace_context     text,
  last_error        text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (request_id, page_no),
  CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'dead')),
  CHECK (priority >= 0 AND priority <= 10)
);

-- ---------------------------------------------------------------------------
-- TABLE 3 — raw_manifest  (index over the GCS raw zone)
-- GCS objects are not queryable. This manifest is what makes the raw zone an
-- index rather than an archive.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_manifest (
  raw_uri      text PRIMARY KEY,
  job_id       uuid NOT NULL REFERENCES collector_job(job_id),
  request_id   uuid NOT NULL,
  source       text NOT NULL,
  page_no      integer NOT NULL,
  record_count integer NOT NULL,
  byte_size    bigint NOT NULL,
  sha256       text NOT NULL,
  written_at   timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_collector_job_open_queue
  ON collector_job (source, status, created_at)
  WHERE status IN ('pending', 'in_progress');

CREATE INDEX IF NOT EXISTS idx_collector_job_request_status
  ON collector_job (request_id, status);

CREATE INDEX IF NOT EXISTS idx_collector_job_lease
  ON collector_job (status, lease_expires_at)
  WHERE status = 'in_progress';
