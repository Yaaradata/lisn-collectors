#!/usr/bin/env bash
# Idempotent Cloud Agent install for the LiSN collectors local dev stack.
#
# Prepares everything a developer needs to run the stack locally WITHOUT Google
# Cloud: a local PostgreSQL holding the `collector` state DB and the fake
# `sentinel_mock` DB, a Python venv with pinned deps, all schemas, and seed data.
#
# The raw-zone (GCS) and warehouse (BigQuery) legs are NOT set up here: they need
# Google Cloud credentials + a project/bucket and are wired via .env + secrets.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_VERSION=16
DBPW="postgres"
N_INCIDENTS_DEFAULT=1000

log() { printf '\033[0;36m[install]\033[0m %s\n' "$*"; }

# --- 1. System packages (Postgres + Python venv tooling) --------------------
if ! command -v psql >/dev/null 2>&1 || ! command -v pg_ctlcluster >/dev/null 2>&1; then
  log "Installing PostgreSQL and Python venv tooling"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    postgresql postgresql-client python3-venv python3-dev
else
  log "PostgreSQL already present"
fi

# --- 2. Start Postgres (needed to create DBs / seed during install) ---------
log "Starting PostgreSQL cluster ${PG_VERSION}/main"
sudo pg_ctlcluster "$PG_VERSION" main start 2>/dev/null || sudo service postgresql start || true
for _ in $(seq 1 30); do
  pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break
  sleep 1
done
pg_isready -h 127.0.0.1 -p 5432

# --- 3. Role password, max_connections=200, and the two databases ----------
log "Configuring postgres role and databases"
sudo -u postgres psql -qc "ALTER USER postgres WITH PASSWORD '${DBPW}';"
if [ "$(sudo -u postgres psql -tAc 'SHOW max_connections' | tr -d '[:space:]')" != "200" ]; then
  sudo -u postgres psql -qc "ALTER SYSTEM SET max_connections = 200;"
  sudo pg_ctlcluster "$PG_VERSION" main restart 2>/dev/null || sudo service postgresql restart || true
  for _ in $(seq 1 30); do pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 && break; sleep 1; done
fi
for db in collector sentinel_mock; do
  if [ "$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | tr -d '[:space:]')" != "1" ]; then
    log "Creating database ${db}"
    sudo -u postgres psql -qc "CREATE DATABASE ${db};"
  fi
done

# --- 4. Python venv + pinned dependencies -----------------------------------
if [ ! -x .venv/bin/python ]; then
  log "Creating Python venv"
  python3 -m venv .venv
fi
log "Installing Python dependencies"
./.venv/bin/python -m pip install --upgrade pip -q
./.venv/bin/pip install -q -r requirements.txt

# --- 5. Local .env (gitignored) ---------------------------------------------
if [ ! -f .env ]; then
  log "Writing local .env"
  cat > .env <<'EOF'
# Local development environment for the LiSN collectors stack (gitignored).
# Postgres runs locally (not via the Cloud SQL Auth Proxy).
COLLECTOR_DSN=postgresql://postgres:postgres@127.0.0.1:5432/collector
SENTINEL_MOCK_DSN=postgresql://postgres:postgres@127.0.0.1:5432/sentinel_mock
PROCRASTINATE_APP=collector.app.app

# Worker/API talk to the LOCAL mock Sentinel, never a Cloud Run URL.
SENTINEL_URL=http://127.0.0.1:8081
COLLECTOR_API_URL=http://127.0.0.1:8080
MOCK_SENTINEL_URL=http://127.0.0.1:8081

N_INCIDENTS=1000
DEMO_SOURCE=sentinel

# --- Google Cloud (raw-zone GCS + BigQuery warehouse leg) -------------------
# Only needed for the full fetch->GCS->BigQuery pipeline (make e2e / worker task
# body). Provide Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS
# or a mounted key) and fill these in to exercise the warehouse leg locally.
PROJECT=
RAW_BUCKET=
EOF
fi

set -a
# shellcheck disable=SC1091
source .env
set +a
export PGPASSWORD="$DBPW"
export PYTHONPATH="$REPO_ROOT"
export PROCRASTINATE_APP="collector.app.app"

# --- 6. Schemas (idempotent) ------------------------------------------------
log "Applying collector schema"
psql -h 127.0.0.1 -U postgres -d collector -v ON_ERROR_STOP=1 -f sql/001_collector.sql >/dev/null

if [ "$(psql -h 127.0.0.1 -U postgres -d collector -tAc "SELECT to_regclass('public.procrastinate_jobs')" | tr -d '[:space:]')" = "" ]; then
  log "Applying Procrastinate schema"
  ./.venv/bin/python -m procrastinate schema --apply
else
  log "Procrastinate schema already applied"
fi

log "Applying sentinel_mock schema"
psql -h 127.0.0.1 -U postgres -d sentinel_mock -v ON_ERROR_STOP=1 -f sql/002_sentinel_mock.sql >/dev/null

# --- 7. Seed the fake Sentinel data (only if not already seeded) ------------
CURRENT="$(psql -h 127.0.0.1 -U postgres -d sentinel_mock -tAc 'SELECT count(*) FROM sentinel_incident' 2>/dev/null | tr -d '[:space:]' || echo 0)"
WANT="${N_INCIDENTS:-$N_INCIDENTS_DEFAULT}"
if [ "$CURRENT" != "$WANT" ]; then
  log "Seeding ${WANT} incidents into sentinel_mock"
  ./.venv/bin/python -m mock.seed_sentinel
else
  log "sentinel_mock already seeded with ${CURRENT} incidents"
fi

log "Install complete."
