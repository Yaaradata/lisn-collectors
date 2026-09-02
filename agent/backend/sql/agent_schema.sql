-- Agent conversation state. Lives in the collector Cloud SQL instance but is
-- namespaced with agent_ so it is clearly NOT part of the collector schema
-- (collector_request / collector_job / discovery_window / …).
--
-- Apply with:
--   psql "$COLLECTOR_DSN" -f agent/backend/sql/agent_schema.sql
-- Do NOT add this to the collector's sql/ folder.

CREATE TABLE IF NOT EXISTS agent_session (
  session_id   text PRIMARY KEY,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE agent_session IS
  'Diagnostic agent chat sessions. Ephemeral Cloud Run instances reload '
  'history from here — never keep conversation state only in memory.';

CREATE TABLE IF NOT EXISTS agent_message (
  message_id   bigserial PRIMARY KEY,
  session_id   text NOT NULL REFERENCES agent_session(session_id) ON DELETE CASCADE,
  -- LangChain message type: human | ai | tool | system
  role         text NOT NULL
               CHECK (role IN ('human', 'ai', 'tool', 'system')),
  content      text NOT NULL DEFAULT '',
  -- Full serialised payload for round-trip (tool_calls, tool_call_id, name, …)
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_message_session_created
  ON agent_message (session_id, created_at);

COMMENT ON TABLE agent_message IS
  'Ordered turns for a diagnostic agent session. payload holds the LangChain '
  'message fields needed to resume tool-calling mid-conversation.';
