-- W3C trace context on collector_job (idempotent for already-deployed DBs).
--
-- The API and workers are different processes linked only by this row — there
-- is no HTTP hop to carry headers. Storing the W3C traceparent here is how
-- fetch_page spans stay children of collect_request. Without it every page is
-- its own disconnected root and the fan-out is invisible in SigNoz.

ALTER TABLE collector_job
  ADD COLUMN IF NOT EXISTS trace_context text;

COMMENT ON COLUMN collector_job.trace_context IS
  'W3C Trace Context carrier (JSON with traceparent[/tracestate]) captured '
  'under the API collect_request span. Workers extract it and start fetch_page '
  'as a CHILD of that span — Postgres is the only propagation channel.';
