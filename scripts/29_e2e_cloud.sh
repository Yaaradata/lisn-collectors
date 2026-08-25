#!/usr/bin/env bash

# Sprint 5 exit gate — prove the DEPLOYED stack matches the local baseline
# using the SAME pytest files (parametrised via COLLECTOR_API_URL + token).

source scripts/_common.sh

need gcloud
need curl
need psql
need python3

: "${COLLECTOR_API_URL:?COLLECTOR_API_URL required — run make deploy-services first}"
: "${DEPLOY_SURFACE:?DEPLOY_SURFACE required — run make deploy-preflight first}"
: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${SENTINEL_URL:?SENTINEL_URL required}"
: "${PROJECT:?PROJECT required}"
: "${BUCKET:?BUCKET required}"
: "${RAW_BUCKET:=$BUCKET}"

if [[ "$COLLECTOR_API_URL" == *"127.0.0.1"* || "$COLLECTOR_API_URL" == *"localhost"* ]]; then
  fail "COLLECTOR_API_URL looks local (${COLLECTOR_API_URL}) — point it at Cloud Run"
fi

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

gate_fail() {
  echo "SPRINT 5 GATE: FAILED — $1"
  exit 1
}

export COLLECTOR_API_TOKEN
COLLECTOR_API_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
if [[ -z "$COLLECTOR_API_TOKEN" ]]; then
  # Fallback: if /health works without a token (temporary allUsers for laptop
  # gates when print-identity-token is unavailable), proceed without Bearer.
  if curl -sf "${COLLECTOR_API_URL}/health" >/dev/null 2>&1; then
    warn "No identity token — API /health is open without auth; continuing"
  else
    fail "gcloud auth print-identity-token failed and API requires auth — re-run gcloud auth login"
  fi
fi

# Tests hit the deployed mock for /health when MOCK_SENTINEL_URL matches SENTINEL_URL.
export MOCK_SENTINEL_URL="${SENTINEL_URL}"
export RAW_BUCKET
export PYTHONPATH="${PYTHONPATH:-.}"

ok "DEPLOY_SURFACE=${DEPLOY_SURFACE}"
ok "COLLECTOR_API_URL=${COLLECTOR_API_URL}"
ok "SENTINEL_URL=${SENTINEL_URL}"

# ---------------------------------------------------------------------------
# No local workers — otherwise a laptop process could produce a false green.
# ---------------------------------------------------------------------------
ok "Checking for local procrastinate workers"
LOCAL_WORKERS="$(
  ps aux 2>/dev/null | grep -E '[p]ython.*-m procrastinate worker|[p]rocrastinate worker' || true
)"
# Windows: look for real python worker processes, ignore wmic/grep themselves.
if [[ -z "$LOCAL_WORKERS" ]] && command -v wmic >/dev/null 2>&1; then
  LOCAL_WORKERS="$(
    wmic process where "name='python.exe' or name='python3.exe'" get ProcessId,CommandLine 2>/dev/null \
      | grep -i 'procrastinate' | grep -i 'worker' | grep -v -i 'wmic' || true
  )"
fi
if [[ -n "$(echo "$LOCAL_WORKERS" | tr -d '[:space:]')" ]]; then
  echo "$LOCAL_WORKERS"
  gate_fail "local procrastinate worker process detected — stop it before e2e-cloud (false green risk)"
fi
ok "No local procrastinate worker processes"

# ---------------------------------------------------------------------------
# Start deployed workers briefly to confirm the surface, then reset
# ---------------------------------------------------------------------------
ok "Starting deployed workers (pre-reset smoke)"
bash scripts/28_workers_control.sh start || gate_fail "workers start"

ok "Poll /v1/health/detail until live_workers >= 4 (120s)"
deadline=$((SECONDS + 120))
LIVE=0
DETAIL=""
while (( SECONDS < deadline )); do
  DETAIL="$(
    curl -sS -H "Authorization: Bearer ${COLLECTOR_API_TOKEN}" \
      "${COLLECTOR_API_URL}/v1/health/detail" || true
  )"
  LIVE="$(
    printf '%s' "$DETAIL" | "$PY" -c '
import json,sys
try:
    print(int(json.load(sys.stdin).get("live_workers") or 0))
except Exception:
    print(0)
' 2>/dev/null || echo 0
  )"
  echo "  live_workers=${LIVE}"
  if (( LIVE >= 4 )); then
    break
  fi
  sleep 5
done
(( LIVE >= 4 )) || gate_fail "live_workers >= 4 timed out (got ${LIVE})"

# ---------------------------------------------------------------------------
# Reset state — empty tables / prefixes so the run is comparable to Pass 1.
# Workers must be cancelled and idle BEFORE truncating procrastinate_workers
# (FK: procrastinate_jobs.worker_id → procrastinate_workers.id). Shared path:
# scripts/33_reset_collector.sh
# ---------------------------------------------------------------------------
ok "Reset collector DB / GCS raw / BigQuery landing (stop workers first)"
bash scripts/33_reset_collector.sh --restart || gate_fail "reset failed"

ok "Poll /v1/health/detail until live_workers >= 4 after reset (120s)"
deadline=$((SECONDS + 120))
LIVE=0
while (( SECONDS < deadline )); do
  DETAIL="$(
    curl -sS -H "Authorization: Bearer ${COLLECTOR_API_TOKEN}" \
      "${COLLECTOR_API_URL}/v1/health/detail" || true
  )"
  LIVE="$(
    printf '%s' "$DETAIL" | "$PY" -c '
import json,sys
try:
    print(int(json.load(sys.stdin).get("live_workers") or 0))
except Exception:
    print(0)
' 2>/dev/null || echo 0
  )"
  echo "  live_workers=${LIVE}"
  if (( LIVE >= 4 )); then
    break
  fi
  sleep 5
done
(( LIVE >= 4 )) || gate_fail "live_workers >= 4 after reset timed out (got ${LIVE})"

# ---------------------------------------------------------------------------
# Same tests as local
# ---------------------------------------------------------------------------
ok "pytest tests/test_end_to_end.py -v"
set +e
"$PY" -m pytest tests/test_end_to_end.py -v
E2E_RC=$?
set -e
(( E2E_RC == 0 )) || gate_fail "test_end_to_end.py exit ${E2E_RC}"

ok "pytest tests/test_failures.py -v"
set +e
"$PY" -m pytest tests/test_failures.py -v
FAIL_RC=$?
set -e
(( FAIL_RC == 0 )) || gate_fail "test_failures.py exit ${FAIL_RC}"

# ---------------------------------------------------------------------------
# Compare to local baseline (Pass 1)
#   20 pages all done · 20 GCS objects · ~2481 BigQuery rows ·
#   1000 distinct ids · reconcile 0
# ---------------------------------------------------------------------------
ok "Compare against local baseline"
LATEST_REQ="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT request_id::text FROM collector_request ORDER BY created_at DESC LIMIT 1;"
)"
[[ -n "$LATEST_REQ" ]] || gate_fail "no collector_request rows after tests"

DONE_N="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT count(*) FROM collector_job WHERE request_id='${LATEST_REQ}'::uuid AND status='done';"
)"
TOTAL_N="$(
  psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -tAc \
    "SELECT count(*) FROM collector_job WHERE request_id='${LATEST_REQ}'::uuid;"
)"
echo "collector_job done/total for ${LATEST_REQ}: ${DONE_N}/${TOTAL_N}"
[[ "$DONE_N" == "20" && "$TOTAL_N" == "20" ]] || gate_fail "expected 20/20 pages done, got ${DONE_N}/${TOTAL_N}"

GCS_N="$("$PY" - <<PY
import os
from google.cloud import storage
rid = "${LATEST_REQ}"
bucket = os.environ.get("BUCKET") or os.environ["RAW_BUCKET"]
prefix = "raw/source=sentinel/"
needle = f"request={rid}/"
client = storage.Client(project=os.environ.get("PROJECT"))
print(sum(1 for b in client.list_blobs(bucket, prefix=prefix) if needle in b.name))
PY
)"
echo "GCS objects for request: ${GCS_N}"
[[ "$GCS_N" == "20" ]] || gate_fail "expected 20 GCS objects, got ${GCS_N}"

BQ_OUT="$("$PY" - <<'PY'
import os
from google.cloud import bigquery
project = os.environ["PROJECT"]
client = bigquery.Client(project=project)
row = list(client.query(
    f"SELECT count(*) AS n, count(DISTINCT id) AS d FROM `{project}.sentinel_raw.incidents`"
).result())[0]
print(f"{row.n} {row.d}")
PY
)"
BQ_N="$(echo "$BQ_OUT" | awk '{print $1}')"
BQ_D="$(echo "$BQ_OUT" | awk '{print $2}')"
echo "BigQuery rows=${BQ_N} distinct_ids=${BQ_D}"
[[ "$BQ_D" == "1000" ]] || gate_fail "expected 1000 distinct ids, got ${BQ_D}"
# ~2481 (±20% of thread-explosion baseline)
"$PY" - <<PY || gate_fail "BigQuery row count ${BQ_N} not near ~2481"
n = int("${BQ_N}")
lo, hi = int(2481 * 0.8), int(2481 * 1.2)
assert lo <= n <= hi, (n, lo, hi)
print(f"bq_rows_ok n={n} window=[{lo},{hi}]")
PY

RECONCILE="$(
  curl -sS -H "Authorization: Bearer ${COLLECTOR_API_TOKEN}" \
    "${COLLECTOR_API_URL}/v1/reconcile?minutes=0"
)"
echo "reconcile: ${RECONCILE}"
UNLOADED="$(
  printf '%s' "$RECONCILE" | "$PY" -c 'import json,sys; print(int(json.load(sys.stdin).get("unloaded") or 0))'
)"
[[ "$UNLOADED" == "0" ]] || gate_fail "reconcile unloaded=${UNLOADED} (expected 0)"

ok "Owners on latest request (expect three task indices for sentinel)"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 <<SQL
SELECT owner, count(*) AS jobs
FROM collector_job
WHERE request_id = '${LATEST_REQ}'::uuid
GROUP BY owner
ORDER BY owner NULLS LAST;
SQL

echo ""
echo "SPRINT 5 GATE: PASSED"
ok "Workers left running for the demo (DEPLOY_SURFACE=${DEPLOY_SURFACE})"
