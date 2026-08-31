#!/usr/bin/env bash
# VERIFY: worker retries against an unreachable DB, then recovers when the
# host becomes reachable. Does NOT stop lisn-collector-db — uses a local
# cloud-sql-proxy on an otherwise-closed port as the "host".
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source .env
set +a

PY="${PWD}/.venv/Scripts/python.exe"
PROXY="${PWD}/.tools/cloud-sql-proxy.exe"
PORT=15432
LOG="${PWD}/.tmp_db_retry_verify.log"
PROXY_LOG="${PWD}/.tmp_db_retry_proxy.log"
: >"$LOG"
: >"$PROXY_LOG"

# Make sure nothing is already on the port.
if (echo >/dev/tcp/127.0.0.1/${PORT}) >/dev/null 2>&1; then
  echo "port ${PORT} already in use — refuse to clobber" >&2
  exit 1
fi

DSN="postgresql://postgres:${DBPW}@127.0.0.1:${PORT}/collector"
export COLLECTOR_DSN="$DSN"
export COLLECTOR_DB_PREFLIGHT=1
export DB_CONNECT_TIMEOUT_S=120
export PROCRASTINATE_APP=collector.app.app
export PYTHONPATH="$PWD"
export COLLECTOR_SOURCE=sentinel

echo "=== starting worker against unreachable 127.0.0.1:${PORT} ===" | tee -a "$LOG"
"$PY" -m procrastinate worker -q sentinel -c 1 --delete-jobs never \
  >>"$LOG" 2>&1 &
WORKER_PID=$!
echo "worker_pid=${WORKER_PID}" | tee -a "$LOG"

# Let a few retry attempts land (1s + 2s + 4s backoff + connect_timeout).
sleep 20

if ! grep -q "database connect attempt" "$LOG"; then
  echo "FAIL: no retry attempt logs after 20s" >&2
  kill "$WORKER_PID" 2>/dev/null || true
  cat "$LOG" >&2
  exit 1
fi
echo "=== retries observed; bringing proxy up on :${PORT} ===" | tee -a "$LOG"

"$PROXY" --address 127.0.0.1 --port "$PORT" \
  --credentials-file "${APPDATA}/gcloud/application_default_credentials.json" \
  "${CONN}" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
echo "proxy_pid=${PROXY_PID}" | tee -a "$LOG"

# Wait for reachable log or worker to start consuming.
deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
  if grep -q "database reachable" "$LOG"; then
    break
  fi
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "FAIL: worker exited before becoming reachable" >&2
    cat "$LOG" >&2
    kill "$PROXY_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 2
done

if ! grep -q "database reachable" "$LOG"; then
  echo "FAIL: never saw 'database reachable'" >&2
  cat "$LOG" >&2
  kill "$WORKER_PID" "$PROXY_PID" 2>/dev/null || true
  exit 1
fi

# Give the procrastinate worker a moment to finish pool open / register.
sleep 8

echo "=== mid-run drop: kill proxy for 45s, then restore ===" | tee -a "$LOG"
kill "$PROXY_PID" 2>/dev/null || true
wait "$PROXY_PID" 2>/dev/null || true
sleep 45

# Is the worker still alive after mid-run unavailability?
if ! kill -0 "$WORKER_PID" 2>/dev/null; then
  echo "MID_RUN_RESULT=worker_exited" | tee -a "$LOG"
  MID_EXITED=1
else
  echo "MID_RUN_RESULT=worker_still_alive" | tee -a "$LOG"
  MID_EXITED=0
fi

"$PROXY" --address 127.0.0.1 --port "$PORT" \
  --credentials-file "${APPDATA}/gcloud/application_default_credentials.json" \
  "${CONN}" >>"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
sleep 15

if [[ "$MID_EXITED" -eq 0 ]]; then
  if kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "MID_RUN_RESULT=worker_survived_and_still_alive_after_restore" | tee -a "$LOG"
  fi
fi

echo "=== shutting down ===" | tee -a "$LOG"
kill "$WORKER_PID" "$PROXY_PID" 2>/dev/null || true
wait "$WORKER_PID" 2>/dev/null || true
wait "$PROXY_PID" 2>/dev/null || true

echo
echo "----- worker log (verify) -----"
cat "$LOG"
