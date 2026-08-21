#!/usr/bin/env bash

# Killswitch for a single collector source.
#
# If Flipkart calls during Big Billion Day and says stop hitting Sentinel, we
# need a lever that works in seconds and affects only that source. Cancelling
# jobs one at a time is the wrong instrument — it does not abort an HTTP call
# already in flight and it does not stop the next one starting.
#
# Sprint 5 production equivalent: with Cloud Run the lever is instance count —
# `--instances=0` to stop a source, `--instances=3` to resume. The flag approach
# here is the local-development stand-in, and both should exist because the flag
# also works while workers are running.
#
# Usage: ./scripts/11_killswitch.sh <pause|resume|drain|status> [source]

source scripts/_common.sh

need psql

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"

ACTION="${1:-}"
SOURCE="${2:-}"
# Single-quote for SQL literals (source names are identifiers like sentinel).
sql_literal() {
  printf "%s" "${1//\'/\'\'}"
}

ensure_control_table() {
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -q <<'SQL'
CREATE TABLE IF NOT EXISTS collector_control (
  source text PRIMARY KEY,
  paused boolean NOT NULL DEFAULT false,
  updated_at timestamptz NOT NULL DEFAULT now()
);
SQL
}

usage() {
  fail "Usage: $0 <pause|resume|drain|status> [source]"
}

require_source() {
  if [[ -z "${SOURCE}" ]]; then
    fail "source is required for action '${ACTION}'"
  fi
}

do_pause() {
  require_source
  ensure_control_table
  src_sql="$(sql_literal "${SOURCE}")"
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO collector_control (source, paused, updated_at)
VALUES ('${src_sql}', true, now())
ON CONFLICT (source) DO UPDATE
  SET paused = true,
      updated_at = now();
SQL
  ok "paused source=${SOURCE}"
}

do_resume() {
  require_source
  ensure_control_table
  src_sql="$(sql_literal "${SOURCE}")"
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -q <<SQL
INSERT INTO collector_control (source, paused, updated_at)
VALUES ('${src_sql}', false, now())
ON CONFLICT (source) DO UPDATE
  SET paused = false,
      updated_at = now();
SQL
  ok "resumed source=${SOURCE}"
}

do_drain() {
  require_source
  ensure_control_table
  src_sql="$(sql_literal "${SOURCE}")"
  ok "draining source=${SOURCE} (pending + in_progress → 0)"
  while true; do
    counts="$(
      psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc "
        SELECT
          coalesce(sum(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
          coalesce(sum(CASE WHEN status = 'in_progress' THEN 1 ELSE 0 END), 0)
        FROM collector_job
        WHERE source = '${src_sql}'
      "
    )"
    pending="${counts%%|*}"
    in_progress="${counts##*|}"
    pending="$(echo "${pending}" | tr -d '[:space:]')"
    in_progress="$(echo "${in_progress}" | tr -d '[:space:]')"
    remaining=$((pending + in_progress))
    printf 'source=%s pending=%s in_progress=%s remaining=%s\n' \
      "${SOURCE}" "${pending}" "${in_progress}" "${remaining}"
    if [[ "${remaining}" -eq 0 ]]; then
      ok "drain complete for source=${SOURCE}"
      return 0
    fi
    sleep 2
  done
}

do_status() {
  ensure_control_table
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
WITH sources AS (
  SELECT source FROM collector_job
  UNION
  SELECT source FROM collector_control
),
job_counts AS (
  SELECT
    source,
    count(*) FILTER (WHERE status = 'pending')     AS pending,
    count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
    count(*) FILTER (WHERE status = 'done')        AS done,
    count(*) FILTER (WHERE status = 'dead')        AS dead
  FROM collector_job
  GROUP BY source
),
live AS (
  SELECT
    c.source,
    count(DISTINCT w.id)::int AS live_workers
  FROM procrastinate_workers w
  JOIN procrastinate_jobs j ON j.worker_id = w.id
  JOIN collector_job c ON c.job_id::text = j.args->>'job_id'
  WHERE now() - w.last_heartbeat < interval '60 seconds'
  GROUP BY c.source
)
SELECT
  s.source,
  coalesce(cc.paused, false) AS paused,
  coalesce(jc.pending, 0) AS pending,
  coalesce(jc.in_progress, 0) AS in_progress,
  coalesce(jc.done, 0) AS done,
  coalesce(jc.dead, 0) AS dead,
  coalesce(l.live_workers, 0) AS live_workers
FROM sources s
LEFT JOIN collector_control cc ON cc.source = s.source
LEFT JOIN job_counts jc ON jc.source = s.source
LEFT JOIN live l ON l.source = s.source
ORDER BY s.source;
SQL
}

case "${ACTION}" in
  pause)  do_pause ;;
  resume) do_resume ;;
  drain)  do_drain ;;
  status) do_status ;;
  *)      usage ;;
esac
