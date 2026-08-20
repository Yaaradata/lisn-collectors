#!/usr/bin/env bash

# Sprint 3 exit gate: boot local stack, run e2e pytest, tear down.

source scripts/_common.sh

need curl
need python3

: "${COLLECTOR_DSN:?COLLECTOR_DSN required}"
: "${SENTINEL_MOCK_DSN:?SENTINEL_MOCK_DSN required}"
: "${RAW_BUCKET:?RAW_BUCKET required}"
: "${PROJECT:?PROJECT required}"
: "${CONN:?CONN required}"

ROOT="$(pwd)"
export PYTHONPATH="${ROOT}"
# Worker/API must hit the LOCAL mock, not the internal Cloud Run URL.
export SENTINEL_URL="http://127.0.0.1:8081"
export COLLECTOR_API_URL="http://127.0.0.1:8080"
export MOCK_SENTINEL_URL="http://127.0.0.1:8081"
export PROCRASTINATE_APP="collector.app.app"

if [[ -x .venv/Scripts/python.exe ]]; then
  PY=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

MOCK_PID=""
API_PID=""
WORKER_PID=""
PROXY_PID=""

cleanup() {
  for pid in "$WORKER_PID" "$API_PID" "$MOCK_PID" "$PROXY_PID"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

gate_fail() {
  echo "SPRINT 3 GATE: FAILED — $1"
  exit 1
}

ok "Ensuring Cloud SQL Auth Proxy on 5432"
if ! (echo >/dev/tcp/127.0.0.1/5432) >/dev/null 2>&1; then
  PROXY_BIN="./cloud-sql-proxy.exe"
  [[ -f "$PROXY_BIN" ]] || PROXY_BIN="./cloud-sql-proxy"
  [[ -f "$PROXY_BIN" ]] || fail "cloud-sql-proxy binary missing — run scripts/05_smoke.sh once"
  "$PROXY_BIN" "$CONN" --port 5432 >/tmp/e2e-proxy.log 2>&1 &
  PROXY_PID=$!
  sleep 4
fi

ok "Ensuring collector + Procrastinate schemas exist"
set +e
"$PY" - <<'PY'
import os, sys, psycopg
dsn = os.environ["COLLECTOR_DSN"]
with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.collector_job')")
        if not cur.fetchone()[0]:
            print("collector_job missing — run make collector-schema", file=sys.stderr)
            sys.exit(1)
        cur.execute("SELECT to_regclass('public.procrastinate_jobs')")
        if not cur.fetchone()[0]:
            sys.exit(2)
print("schemas OK")
PY
SCHEMA_RC=$?
set -e
if (( SCHEMA_RC == 1 )); then
  gate_fail "collector schema missing"
elif (( SCHEMA_RC == 2 )); then
  warn "Procrastinate schema missing — applying"
  "$PY" -m procrastinate schema --apply || gate_fail "procrastinate schema apply"
elif (( SCHEMA_RC != 0 )); then
  gate_fail "schema check failed"
fi

ok "Starting mock Sentinel on :8081"
"$PY" -m uvicorn mock.sentinel_api:app --host 127.0.0.1 --port 8081 >/tmp/e2e-mock.log 2>&1 &
MOCK_PID=$!

ok "Starting request API on :8080"
"$PY" -m uvicorn collector.api:api --host 127.0.0.1 --port 8080 >/tmp/e2e-api.log 2>&1 &
API_PID=$!

ok "Starting Procrastinate worker (sentinel, -c 1)"
"$PY" -m procrastinate worker -q sentinel -c 1 --delete-jobs never >/tmp/e2e-worker.log 2>&1 &
WORKER_PID=$!

ok "Waiting for mock + API health"
deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
  if curl -sf "${MOCK_SENTINEL_URL}/health" >/dev/null \
    && curl -sf "${COLLECTOR_API_URL}/health" >/dev/null; then
    break
  fi
  sleep 1
done
curl -sf "${MOCK_SENTINEL_URL}/health" >/dev/null || gate_fail "mock health"
curl -sf "${COLLECTOR_API_URL}/health" >/dev/null || gate_fail "api health"
kill -0 "$WORKER_PID" 2>/dev/null || gate_fail "worker process died (see /tmp/e2e-worker.log)"
ok "All three processes up (worker pid=${WORKER_PID})"

ok "Running pytest tests/test_end_to_end.py -v"
set +e
"$PY" -m pytest tests/test_end_to_end.py -v
PYTEST_RC=$?
set -e

if (( PYTEST_RC != 0 )); then
  gate_fail "pytest exit ${PYTEST_RC} (see failures above; logs in /tmp/e2e-*.log)"
fi

echo "SPRINT 3 GATE: PASSED"
