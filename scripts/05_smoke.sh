#!/usr/bin/env bash

source scripts/_common.sh

TRACE_FILE="docs/trace/S1.md"
mkdir -p docs/trace

FAILED_CHECK=""
PROXY_PID=""

trace_line() {
  local result="$1"
  local check="$2"
  local details="$3"
  if [[ ! -f "$TRACE_FILE" ]]; then
    printf '| Result | Check | Details |\n|---|---|---|\n' >"$TRACE_FILE"
  fi
  printf '| %s | %s | %s |\n' "$result" "$check" "$details" >>"$TRACE_FILE"
}

upsert_env() {
  local key="$1"
  local value="$2"
  local tmp
  tmp="$(mktemp)"
  if [[ -f .env ]] && grep -q "^${key}=" .env; then
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      index($0, k "=") == 1 {
        print k "=" v
        done=1
        next
      }
      { print }
      END { if (!done) print k "=" v }
    ' .env >"$tmp" && mv "$tmp" .env
  else
    printf '%s=%s\n' "$key" "$value" >>.env
  fi
}

reload_env() {
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
}

cleanup() {
  if [[ -n "${PROXY_PID}" ]] && kill -0 "${PROXY_PID}" 2>/dev/null; then
    kill "${PROXY_PID}" 2>/dev/null || true
    wait "${PROXY_PID}" 2>/dev/null || true
  fi
}

gate_fail() {
  FAILED_CHECK="$1"
  trace_line "FAIL" "sprint1-gate" "${FAILED_CHECK}"
  echo "SPRINT 1 GATE: FAILED — ${FAILED_CHECK} — do not proceed"
  exit 1
}

trap cleanup EXIT

need gcloud
need bq
need psql
need curl

if [[ -x .venv/Scripts/python.exe ]]; then
  PYTHON_BIN=".venv/Scripts/python.exe"
elif [[ -x .venv/bin/python ]]; then
  PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  fail "Missing Python (expected .venv or python3 on PATH)"
fi
ok "Using Python: ${PYTHON_BIN}"

: "${PROJECT:?PROJECT is required in .env}"
: "${CONN:?CONN is required in .env (run scripts/01_database.sh first)}"
: "${DBPW:?DBPW is required in .env (run scripts/01_database.sh first)}"
: "${BUCKET:?BUCKET is required in .env}"
: "${RAW_BUCKET:?RAW_BUCKET is required in .env}"
: "${DEMO_SOURCE:?DEMO_SOURCE is required in .env}"

echo "## Smoke / Sprint 1 gate $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# STEP 1 — Cloud SQL Auth Proxy
# ---------------------------------------------------------------------------
ok "STEP 1 — Cloud SQL Auth Proxy"
# Prefer a platform-native binary so Git Bash on Windows can run the gate.
case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    PROXY_BIN="./cloud-sql-proxy.exe"
    PROXY_URL="https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.13.0/cloud-sql-proxy.x64.exe"
    PROXY_LABEL="v2.13.0 windows amd64"
    ;;
  Darwin)
    PROXY_BIN="./cloud-sql-proxy"
    PROXY_URL="https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.13.0/cloud-sql-proxy.darwin.amd64"
    PROXY_LABEL="v2.13.0 darwin amd64"
    ;;
  *)
    PROXY_BIN="./cloud-sql-proxy"
    PROXY_URL="https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.13.0/cloud-sql-proxy.linux.amd64"
    PROXY_LABEL="v2.13.0 linux amd64"
    ;;
esac

if [[ ! -f "$PROXY_BIN" ]]; then
  ok "Downloading cloud-sql-proxy ${PROXY_LABEL}"
  curl -fsSL -o "$PROXY_BIN" "$PROXY_URL"
  chmod +x "$PROXY_BIN"
  trace_line "PASS" "proxy-download" "$PROXY_LABEL"
else
  ok "cloud-sql-proxy binary already present (${PROXY_BIN})"
  trace_line "PASS" "proxy-download" "already present"
fi

"$PROXY_BIN" "$CONN" --port 5432 &
PROXY_PID=$!
sleep 5

if ! kill -0 "$PROXY_PID" 2>/dev/null; then
  gate_fail "Cloud SQL Auth Proxy failed to stay running"
fi
ok "Cloud SQL Auth Proxy running on 5432 (pid ${PROXY_PID})"
trace_line "PASS" "proxy" "listening on 5432"

# ---------------------------------------------------------------------------
# STEP 2 — Local DSN forms for Sprints 2 and 3
# ---------------------------------------------------------------------------
ok "STEP 2 — Write local DSNs to .env"
COLLECTOR_DSN_VALUE="postgresql://postgres:${DBPW}@127.0.0.1:5432/collector"
SENTINEL_MOCK_DSN_VALUE="postgresql://postgres:${DBPW}@127.0.0.1:5432/sentinel_mock"
upsert_env "COLLECTOR_DSN" "$COLLECTOR_DSN_VALUE"
upsert_env "SENTINEL_MOCK_DSN" "$SENTINEL_MOCK_DSN_VALUE"
reload_env
ok "COLLECTOR_DSN=$(printf '%s' "$COLLECTOR_DSN" | mask)"
ok "SENTINEL_MOCK_DSN=$(printf '%s' "$SENTINEL_MOCK_DSN" | mask)"
trace_line "PASS" "COLLECTOR_DSN" "$(printf '%s' "$COLLECTOR_DSN" | mask)"
trace_line "PASS" "SENTINEL_MOCK_DSN" "$(printf '%s' "$SENTINEL_MOCK_DSN" | mask)"

export PGPASSWORD="$DBPW"

psql_collector() {
  psql -h 127.0.0.1 -p 5432 -U postgres -d collector -v ON_ERROR_STOP=1 "$@"
}

psql_mock() {
  psql -h 127.0.0.1 -p 5432 -U postgres -d sentinel_mock -v ON_ERROR_STOP=1 "$@"
}

# ---------------------------------------------------------------------------
# STEP 3 — Postgres connectivity / settings
# ---------------------------------------------------------------------------
ok "STEP 3 — Postgres"
version_out="$(psql_collector -tAc 'SELECT version()' 2>/dev/null || true)"
if [[ -z "$version_out" ]]; then
  gate_fail "Postgres SELECT version() on collector"
fi
ok "collector SELECT version(): ${version_out}"
trace_line "PASS" "postgres-version" "$version_out"
verify "SELECT version() on collector" "non-empty" "non-empty"

max_conn="$(psql_collector -tAc 'SHOW max_connections' | tr -d '[:space:]')"
if [[ "$max_conn" != "200" ]]; then
  verify "max_connections" "200" "$max_conn"
  gate_fail "SHOW max_connections (expected 200, got ${max_conn})"
fi
verify "max_connections" "200" "$max_conn"

mock_db="$(psql_mock -tAc 'SELECT current_database()' | tr -d '[:space:]')"
if [[ "$mock_db" != "sentinel_mock" ]]; then
  verify "current_database() on sentinel_mock" "sentinel_mock" "$mock_db"
  gate_fail "SELECT current_database() on sentinel_mock"
fi
verify "current_database() on sentinel_mock" "sentinel_mock" "$mock_db"

# ---------------------------------------------------------------------------
# STEP 4 — DDL rights on collector
# ---------------------------------------------------------------------------
ok "STEP 4 — DDL rights on collector"
# Sprint 3 creates real tables here, so DDL rights must be proven now.
if ! psql_collector -c 'CREATE TABLE _smoke (id integer PRIMARY KEY);' >/dev/null; then
  gate_fail "DDL CREATE TABLE _smoke"
fi
ok "CREATE TABLE _smoke"
if ! psql_collector -c 'INSERT INTO _smoke (id) VALUES (1);' >/dev/null; then
  gate_fail "DDL INSERT into _smoke"
fi
ok "INSERT into _smoke"
smoke_count="$(psql_collector -tAc 'SELECT count(*) FROM _smoke;' | tr -d '[:space:]')"
if [[ "$smoke_count" != "1" ]]; then
  verify "SELECT count(*) FROM _smoke" "1" "$smoke_count"
  gate_fail "DDL SELECT count from _smoke"
fi
verify "SELECT count(*) FROM _smoke" "1" "$smoke_count"
if ! psql_collector -c 'DROP TABLE _smoke;' >/dev/null; then
  gate_fail "DDL DROP TABLE _smoke"
fi
ok "DROP TABLE _smoke"
trace_line "PASS" "ddl-rights" "CREATE INSERT SELECT DROP all succeeded"

# ---------------------------------------------------------------------------
# STEP 5 — GCS round trip under real prefix shape
# ---------------------------------------------------------------------------
ok "STEP 5 — GCS round trip"
SMOKE_OBJECT="raw/source=${DEMO_SOURCE}/_smoke.txt"
SMOKE_URI="gs://${BUCKET}/${SMOKE_OBJECT}"
SMOKE_CONTENTS="sprint1-smoke-$(date -u +%Y%m%dT%H%M%SZ)"

printf '%s' "$SMOKE_CONTENTS" | gcloud storage cp - "$SMOKE_URI" --project="$PROJECT" >/dev/null
read_back="$(gcloud storage cat "$SMOKE_URI" --project="$PROJECT")"
if [[ "$read_back" != "$SMOKE_CONTENTS" ]]; then
  verify "GCS round trip contents" "$SMOKE_CONTENTS" "$read_back"
  gate_fail "GCS round trip read-back mismatch"
fi
verify "GCS round trip contents" "$SMOKE_CONTENTS" "$read_back"
gcloud storage rm "$SMOKE_URI" --project="$PROJECT" >/dev/null
ok "GCS object written, read, asserted, deleted at ${SMOKE_OBJECT}"
trace_line "PASS" "gcs-roundtrip" "$SMOKE_OBJECT"

# ---------------------------------------------------------------------------
# STEP 6 — BigQuery round trip
# ---------------------------------------------------------------------------
ok "STEP 6 — BigQuery round trip"
# Proves both dataEditor and jobUser are working.
if ! bq query --project_id="$PROJECT" --use_legacy_sql=false --format=csv 'SELECT 1' >/dev/null; then
  gate_fail "BigQuery SELECT 1"
fi
ok "BigQuery SELECT 1"

SMOKE_TABLE="${DEMO_SOURCE}_raw._smoke"
if ! bq query \
  --project_id="$PROJECT" \
  --use_legacy_sql=false \
  --destination_table="${PROJECT}:${SMOKE_TABLE}" \
  --replace \
  --format=none \
  'SELECT 1 AS n'; then
  gate_fail "BigQuery create ${SMOKE_TABLE} from query"
fi
ok "Created ${SMOKE_TABLE} from query"

bq_count="$(
  bq query \
    --project_id="$PROJECT" \
    --use_legacy_sql=false \
    --format=csv \
    --max_rows=1 \
    "SELECT count(*) FROM \`${PROJECT}.${DEMO_SOURCE}_raw._smoke\`" \
    | tail -n 1 | tr -d '[:space:]'
)"
if [[ "$bq_count" != "1" ]]; then
  verify "BigQuery count(_smoke)" "1" "$bq_count"
  gate_fail "BigQuery count ${SMOKE_TABLE}"
fi
verify "BigQuery count(_smoke)" "1" "$bq_count"

if ! bq rm -f -t "${PROJECT}:${SMOKE_TABLE}"; then
  gate_fail "BigQuery drop ${SMOKE_TABLE}"
fi
ok "Dropped ${SMOKE_TABLE}"
trace_line "PASS" "bq-roundtrip" "${SMOKE_TABLE}"

# ---------------------------------------------------------------------------
# STEP 7 — Python ADC smoke
# ---------------------------------------------------------------------------
ok "STEP 7 — scripts/smoke_test.py"
export COLLECTOR_DSN SENTINEL_MOCK_DSN RAW_BUCKET PROJECT DEMO_SOURCE
if ! "$PYTHON_BIN" scripts/smoke_test.py; then
  gate_fail "scripts/smoke_test.py"
fi
ok "smoke_test.py passed"
trace_line "PASS" "smoke_test.py" "postgres OK / gcs OK / bigquery OK"

echo "SPRINT 1 GATE: PASSED — proceed to Sprint 2"
trace_line "PASS" "sprint1-gate" "PASSED — proceed to Sprint 2"
