#!/usr/bin/env bash

source scripts/_common.sh

TRACE_FILE="docs/trace/S1.md"
mkdir -p docs/trace

trace_line() {
  local result="$1"
  local check="$2"
  local details="$3"
  if [[ ! -f "$TRACE_FILE" ]]; then
    printf '| Result | Check | Details |\n|---|---|---|\n' >"$TRACE_FILE"
  fi
  printf '| %s | %s | %s |\n' "$result" "$check" "$details" >>"$TRACE_FILE"
}

# Idempotent .env key write: replace existing key or append if missing.
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

need gcloud
need python3

echo "## Database setup $(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$TRACE_FILE"

# ---------------------------------------------------------------------------
# STEP 1 — Password
# ---------------------------------------------------------------------------
ok "STEP 1 — Password"
if [[ -z "${DBPW:-}" ]]; then
  DBPW="$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")"
  upsert_env "DBPW" "$DBPW"
  reload_env
  # Never print the raw password; only a masked DSN-shaped preview.
  ok "Generated DBPW and wrote to .env ($(printf 'postgresql://postgres:%s@host/db' "$DBPW" | mask))"
  trace_line "PASS" "dbpw" "generated and stored in .env ($(printf 'postgresql://postgres:%s@host/db' "$DBPW" | mask))"
else
  ok "DBPW already present in .env ($(printf 'postgresql://postgres:%s@host/db' "$DBPW" | mask))"
  trace_line "PASS" "dbpw" "already present in .env ($(printf 'postgresql://postgres:%s@host/db' "$DBPW" | mask))"
fi

# ---------------------------------------------------------------------------
# STEP 2 — Create instance (skip if exists)
# ---------------------------------------------------------------------------
ok "STEP 2 — Create instance if missing"
if gcloud sql instances describe "$INSTANCE" --project "$PROJECT" >/dev/null 2>&1; then
  warn "Cloud SQL instance ${INSTANCE} already exists; skipping create"
  trace_line "WARN" "instance-create" "${INSTANCE} ALREADY EXISTS"
else
  # max_connections=200 at create time: by the third source we will have several
  # worker pools plus jobs plus the request API plus a developer laptop all
  # holding connections, and this tier's default is low. Raising it later means
  # a restart.
  #
  # PITR + retained-transaction-log-days=7: agreed backup posture — a daily
  # backup at 19:00 IST plus point-in-time restore across the week.
  # ENTERPRISE edition is required for shared-core tiers like db-g1-small;
  # ENTERPRISE_PLUS rejects those tiers.
  gcloud sql instances create "$INSTANCE" \
    --project="$PROJECT" \
    --database-version=POSTGRES_16 \
    --edition=ENTERPRISE \
    --tier=db-g1-small \
    --region="$REGION" \
    --storage-size=20GB \
    --storage-auto-increase \
    --backup-start-time=19:00 \
    --enable-point-in-time-recovery \
    --retained-transaction-log-days=7 \
    --database-flags=max_connections=200
  ok "Cloud SQL instance create requested for ${INSTANCE}"
  trace_line "PASS" "instance-create" "${INSTANCE} create requested"
fi

# ---------------------------------------------------------------------------
# STEP 3 — Wait for RUNNABLE (poll 30s, timeout 20m)
# ---------------------------------------------------------------------------
ok "STEP 3 — Wait for RUNNABLE"
start_ts="$(date +%s)"
timeout_secs=$((20 * 60))
while true; do
  state="$(
    gcloud sql instances describe "$INSTANCE" \
      --project "$PROJECT" \
      --format='value(state)' 2>/dev/null || true
  )"
  now_ts="$(date +%s)"
  elapsed=$((now_ts - start_ts))
  echo "instance=${INSTANCE} state=${state:-UNKNOWN} elapsed=${elapsed}s"

  if [[ "$state" == "RUNNABLE" ]]; then
    ok "Instance ${INSTANCE} is RUNNABLE"
    trace_line "PASS" "instance-state" "RUNNABLE"
    break
  fi

  if (( elapsed >= timeout_secs )); then
    trace_line "FAIL" "instance-state" "timeout after 20m waiting for RUNNABLE"
    fail "Timed out after 20 minutes waiting for ${INSTANCE} to become RUNNABLE. Check Cloud Console > SQL > Operations log."
  fi

  sleep 30
done

# ---------------------------------------------------------------------------
# STEP 4 — Set postgres password
# ---------------------------------------------------------------------------
ok "STEP 4 — Set postgres user password"
gcloud sql users set-password postgres \
  --instance="$INSTANCE" \
  --project="$PROJECT" \
  --password="$DBPW"
ok "postgres password updated ($(printf 'postgresql://postgres:%s@host/db' "$DBPW" | mask))"
trace_line "PASS" "postgres-password" "updated ($(printf 'postgresql://postgres:%s@host/db' "$DBPW" | mask))"

# ---------------------------------------------------------------------------
# STEP 5 — Capture connection name
# ---------------------------------------------------------------------------
ok "STEP 5 — Capture connection name"
CONN_VALUE="$(
  gcloud sql instances describe "$INSTANCE" \
    --project "$PROJECT" \
    --format='value(connectionName)'
)"
upsert_env "CONN" "$CONN_VALUE"
reload_env
ok "CONN=${CONN_VALUE}"
trace_line "PASS" "connection-name" "${CONN_VALUE}"

# ---------------------------------------------------------------------------
# STEP 6 — Create collector and sentinel_mock databases
# ---------------------------------------------------------------------------
ok "STEP 6 — Create collector and sentinel_mock databases (if missing)"
# Deliberately separate databases so no code can ever join across the boundary —
# a join that will be impossible in production, where Sentinel is a system we
# can only read over the network.
# collector: shared by ALL collectors; job table carries a `source` column so
# LiSN can ask for counts across every source in one query.
# sentinel_mock: fake Sentinel data standing in for Flipkart.
db_exists() {
  local db_name="$1"
  gcloud sql databases list \
    --instance="$INSTANCE" \
    --project="$PROJECT" \
    --format='value(name)' | grep -Fxq "$db_name"
}

for db in collector sentinel_mock; do
  if db_exists "$db"; then
    warn "Database ${db} already exists; skipping create"
    trace_line "WARN" "database:${db}" "ALREADY EXISTS"
  else
    gcloud sql databases create "$db" \
      --instance="$INSTANCE" \
      --project="$PROJECT"
    ok "Created database ${db}"
    trace_line "PASS" "database:${db}" "CREATED"
  fi
done

# ---------------------------------------------------------------------------
# STEP 7 — Cloud Run unix-socket DSNs in .env
# ---------------------------------------------------------------------------
ok "STEP 7 — Store Cloud Run socket DSNs in .env"
collector_dsn_socket="postgresql://postgres:${DBPW}@/collector?host=/cloudsql/${CONN_VALUE}"
sentinel_mock_dsn_socket="postgresql://postgres:${DBPW}@/sentinel_mock?host=/cloudsql/${CONN_VALUE}"
upsert_env "COLLECTOR_DSN_SOCKET" "$collector_dsn_socket"
upsert_env "SENTINEL_MOCK_DSN_SOCKET" "$sentinel_mock_dsn_socket"
reload_env
ok "COLLECTOR_DSN_SOCKET=$(printf '%s' "$collector_dsn_socket" | mask)"
ok "SENTINEL_MOCK_DSN_SOCKET=$(printf '%s' "$sentinel_mock_dsn_socket" | mask)"
trace_line "PASS" "COLLECTOR_DSN_SOCKET" "$(printf '%s' "$collector_dsn_socket" | mask)"
trace_line "PASS" "SENTINEL_MOCK_DSN_SOCKET" "$(printf '%s' "$sentinel_mock_dsn_socket" | mask)"

# ---------------------------------------------------------------------------
# VERIFY — empty databases only; no tables created this sprint
# ---------------------------------------------------------------------------
ok "VERIFY — instance settings and databases (no tables created)"
verify "state" "RUNNABLE" "$(
  gcloud sql instances describe "$INSTANCE" --project "$PROJECT" --format='value(state)'
)"
verify "tier" "db-g1-small" "$(
  gcloud sql instances describe "$INSTANCE" --project "$PROJECT" --format='value(settings.tier)'
)"
verify "pointInTimeRecoveryEnabled" "True" "$(
  gcloud sql instances describe "$INSTANCE" \
    --project "$PROJECT" \
    --format='value(settings.backupConfiguration.pointInTimeRecoveryEnabled)'
)"
verify "backup startTime" "19:00" "$(
  gcloud sql instances describe "$INSTANCE" \
    --project "$PROJECT" \
    --format='value(settings.backupConfiguration.startTime)'
)"

db_list="$(
  gcloud sql databases list \
    --instance="$INSTANCE" \
    --project="$PROJECT" \
    --format='value(name)' | tr '\n' ' ' | sed 's/[[:space:]]*$//'
)"
ok "databases listed: ${db_list}"
trace_line "PASS" "databases" "${db_list}"

if db_exists "collector" && db_exists "sentinel_mock"; then
  verify "collector database present" "yes" "yes"
  verify "sentinel_mock database present" "yes" "yes"
else
  verify "collector and sentinel_mock present" "yes" "no"
  fail "Expected both collector and sentinel_mock databases to exist"
fi

ok "Database scaffolding complete (empty databases only; no tables)."
