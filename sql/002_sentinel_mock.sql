-- Mock Sentinel schema for the FAKE Flipkart Sentinel incident system.
-- Targets: database sentinel_mock ONLY (never collector).
--
-- Real Sentinel exports are THREAD-EXPLODED: one incident appears on multiple
-- rows (one per conversation thread entry). In the real dump, a single order
-- occupies four consecutive rows. Model = two tables, one-to-many; the export
-- joins them.

-- ---------------------------------------------------------------------------
-- TABLE 1 — sentinel_incident
-- Snake_case versions of Sentinel's real dotted export names.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentinel_incident (
  id                          text PRIMARY KEY,          -- e.g. IN26072600000830716528
  issue_id                    integer,                   -- e.g. 3267
  issue_name                  text,                      -- e.g. 'Delay in Delivery'
  issue_parent_id             integer,
  issue_parent_name           text,
  issue_grandparent_id        integer,
  issue_grandparent_name      text,
  order_id                    text,                      -- e.g. OD438107336320536100
  -- Identifiers, not quantities. Stored as text so values above 2^53 and
  -- leading-zero forms survive; never float/numeric on the wire or in BQ.
  order_item_id               text,
  order_item_unit_id          text,
  -- tracking_id is deliberately nullable: ~14% of real incidents have no
  -- tracking ID and require a separate FDP lookup; the collector must exercise
  -- that path.
  -- Real prefixes: FMPC, FMPP, FMPN — all match LIKE 'FMP%', which is why
  -- downstream logic uses one pattern rather than three conditions.
  tracking_id                 text,                      -- FMPC/FMPP/FMPN + 10 digits, NULLABLE
  order_item_product_fsn      text,
  incident_score              integer,
  resolution_deadline         timestamptz,
  resolution_deadline_breach  boolean,
  resolution_re_deadline      timestamptz,
  seller_id                   text,
  source                      text,
  status_id                   integer,
  status_status               text,                      -- Solved / Updated / Unresolved
  status_status_type          text,                      -- RESOLVED / UNRESOLVED
  subject                     text,
  updated_on                  timestamptz,
  aging_score                 integer,
  last_updated_by_user        text,
  payment_id                  text,
  booking_id                  text,
  reverse_tracking_id         text,
  return_id                   text,
  queue                       text,                      -- e.g. 'IMS V2'
  assigned_to                 text,
  created_at                  timestamptz
);

-- ---------------------------------------------------------------------------
-- TABLE 2 — sentinel_thread
-- One row per conversation thread entry belonging to an incident.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sentinel_thread (
  thread_id               text PRIMARY KEY,
  incident_id             text NOT NULL REFERENCES sentinel_incident(id) ON DELETE CASCADE,
  channel_id              integer,       -- 5 Outbound, 9 Email, 1005 Proactive
  channel_name            text,
  communication_id        text,          -- identifier; text, never float
  content_type            text,          -- 'text/plain'
  created_at              timestamptz,
  created_by              text,          -- fk_crm_automation, abdul.wahid
  system_thread           boolean,
  thread_entry_type_id    integer,       -- 1,5,6,9,30,1005
  thread_entry_type_name  text,          -- Note, Outbound, Rule Response, Email,
                                         -- Elixir Updates, Proactive
  updated_at              timestamptz,
  updated_by              text
);

-- ---------------------------------------------------------------------------
-- INDEXES
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_sentinel_thread_incident_id
  ON sentinel_thread (incident_id);

CREATE INDEX IF NOT EXISTS idx_sentinel_incident_order_id
  ON sentinel_incident (order_id);

CREATE INDEX IF NOT EXISTS idx_sentinel_incident_tracking_id
  ON sentinel_incident (tracking_id)
  WHERE tracking_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sentinel_incident_issue_name
  ON sentinel_incident (issue_name);

-- Upgrade path: older mocks stored these as numeric. Only alter when still
-- numeric — a fresh text column must not be rewritten (trimming would eat
-- trailing zeros on digit-string IDs like '1000').
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'sentinel_incident'
      AND column_name = 'order_item_id'
      AND data_type = 'numeric'
  ) THEN
    ALTER TABLE sentinel_incident
      ALTER COLUMN order_item_id TYPE text
        USING regexp_replace(order_item_id::text, '\.0+$', ''),
      ALTER COLUMN order_item_unit_id TYPE text
        USING regexp_replace(order_item_unit_id::text, '\.0+$', '');
  END IF;
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'sentinel_thread'
      AND column_name = 'communication_id'
      AND data_type = 'numeric'
  ) THEN
    ALTER TABLE sentinel_thread
      ALTER COLUMN communication_id TYPE text
        USING regexp_replace(communication_id::text, '\.0+$', '');
  END IF;
END$$;
