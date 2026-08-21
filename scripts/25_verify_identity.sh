#!/usr/bin/env bash

# Print distinct collector_job.owner values for the most recent request.
# On a 3-task Cloud Run jobs deploy, expect owners like:
#   sentinel-task0, sentinel-task1, sentinel-task2

source scripts/_common.sh

need psql

: "${COLLECTOR_DSN:?COLLECTOR_DSN required in .env}"

ok "Distinct owners for the most recent collector_request"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<'SQL'
WITH latest AS (
  SELECT request_id
  FROM collector_request
  ORDER BY created_at DESC
  LIMIT 1
)
SELECT
  l.request_id,
  j.owner,
  count(*) AS jobs
FROM latest l
JOIN collector_job j ON j.request_id = l.request_id
GROUP BY l.request_id, j.owner
ORDER BY j.owner NULLS LAST;
SQL
