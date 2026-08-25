#!/usr/bin/env bash
# Idempotent Cloud Agent install: system deps, Python venv, local Postgres,
# schemas, and seed data for the LiSN x Flipkart collectors dev loop.
#
# The production stack runs on GCP (Cloud SQL, GCS, BigQuery, Cloud Run). For a
# self-contained dev environment we substitute a LOCAL Postgres for Cloud SQL
# and run the mock Sentinel service, request API and Procrastinate worker
# directly. The GCS raw zone and BigQuery warehouse still require real GCP
# credentials (see .cursor/README note in the PR); everything else works offline.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '[install] %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1 — System packages: PostgreSQL server + client, Python venv/pip
# ---------------------------------------------------------------------------
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
need_apt=0
command -v psql >/dev/null 2>&1 || need_apt=1
ls /usr/lib/postgresql >/dev/null 2>&1 || need_apt=1
python3 -c 'import ensurepip' >/dev/null 2>&1 || need_apt=1
if [[ "$need_apt" == "1" ]]; then
  log "Installing PostgreSQL + Python venv/pip via apt"
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    postgresql postgresql-client "python${PYVER}-venv" python3-pip
else
  log "PostgreSQL + Python venv already installed"
fi

PGVER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -1 || true)"
if [[ -z "$PGVER" ]]; then
  echo "[install] ERROR: no PostgreSQL cluster config found under /etc/postgresql" >&2
  exit 1
fi
log "Using PostgreSQL major version ${PGVER}"

# ---------------------------------------------------------------------------
# 2 — Configure the cluster (max_connections mirrors the Cloud SQL instance)
# ---------------------------------------------------------------------------
PG_CONF="/etc/postgresql/${PGVER}/main/postgresql.conf"
if ! sudo grep -qE '^\s*max_connections\s*=\s*200' "$PG_CONF"; then
  log "Setting max_connections = 200"
  sudo sed -i "s/^\s*#\?\s*max_connections\s*=.*/max_connections = 200/" "$PG_CONF"
fi

# ---------------------------------------------------------------------------
# 3 — Start the cluster so we can create databases and apply schema
# ---------------------------------------------------------------------------
if ! sudo pg_lsclusters -h 2>/dev/null | awk -v v="$PGVER" '$1==v && $2=="main"{print $4}' | grep -q online; then
  log "Starting PostgreSQL cluster ${PGVER}/main"
  sudo pg_ctlcluster "$PGVER" main start || sudo pg_ctlcluster "$PGVER" main restart
fi

# Wait for the socket to accept connections.
for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done
sudo -u postgres pg_isready

# ---------------------------------------------------------------------------
# 4 — Role password + application databases (all idempotent)
# ---------------------------------------------------------------------------
DBPW="postgres"
log "Ensuring postgres role password"
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "ALTER USER postgres PASSWORD '${DBPW}';" >/dev/null

for db in collector sentinel_mock; do
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    log "Creating database ${db}"
    sudo -u postgres createdb "$db"
  else
    log "Database ${db} already exists"
  fi
done

# ---------------------------------------------------------------------------
# 5 — Python virtual environment + dependencies
# ---------------------------------------------------------------------------
if [[ ! -x .venv/bin/python ]] || ! ./.venv/bin/python -m pip --version >/dev/null 2>&1; then
  log "Creating Python virtual environment (.venv)"
  rm -rf .venv
  python3 -m venv .venv
fi
log "Installing Python dependencies"
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
# 6 — .env for local development (does not overwrite an existing file's secrets)
# ---------------------------------------------------------------------------
COLLECTOR_DSN="postgresql://postgres:${DBPW}@127.0.0.1:5432/collector"
SENTINEL_MOCK_DSN="postgresql://postgres:${DBPW}@127.0.0.1:5432/sentinel_mock"

upsert_env() {
  local key="$1" value="$2"
  touch .env
  if grep -qE "^${key}=" .env; then
    # Preserve a non-empty pre-existing value (e.g. GCP secrets) for these keys.
    case "$key" in
      RAW_BUCKET|PROJECT|PROJECT_NUMBER|BUCKET|IMG|CONN)
        local existing
        existing="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2-)"
        [[ -n "$existing" ]] && return 0
        ;;
    esac
    sed -i "s#^${key}=.*#${key}=${value}#" .env
  else
    printf '%s=%s\n' "$key" "$value" >>.env
  fi
}

log "Writing .env (local DSNs + mock URL)"
upsert_env DBPW "$DBPW"
upsert_env COLLECTOR_DSN "$COLLECTOR_DSN"
upsert_env SENTINEL_MOCK_DSN "$SENTINEL_MOCK_DSN"
upsert_env SENTINEL_URL "http://127.0.0.1:8081"
upsert_env COLLECTOR_API_URL "http://127.0.0.1:8080"
upsert_env MOCK_SENTINEL_URL "http://127.0.0.1:8081"
upsert_env PROCRASTINATE_APP "collector.app.app"
upsert_env DEMO_SOURCE "sentinel"
upsert_env N_INCIDENTS "1000"
# GCP-backed sinks: populated from secrets if provided, else left blank.
upsert_env PROJECT "${PROJECT:-}"
upsert_env RAW_BUCKET "${RAW_BUCKET:-}"

# ---------------------------------------------------------------------------
# 7 — Schemas: collector state, Procrastinate queue, mock Sentinel
# ---------------------------------------------------------------------------
export PGPASSWORD="$DBPW"
log "Applying collector state schema"
psql "$COLLECTOR_DSN" -v ON_ERROR_STOP=1 -q -f sql/001_collector.sql

log "Applying Procrastinate schema"
PYTHONPATH="$REPO_ROOT" PROCRASTINATE_APP="collector.app.app" COLLECTOR_DSN="$COLLECTOR_DSN" \
  ./.venv/bin/python -m procrastinate schema --apply >/dev/null 2>&1 \
  || log "Procrastinate schema already applied"

log "Applying mock Sentinel schema"
psql "$SENTINEL_MOCK_DSN" -v ON_ERROR_STOP=1 -q -f sql/002_sentinel_mock.sql

# ---------------------------------------------------------------------------
# 8 — Seed the mock Sentinel dataset (deterministic, re-runnable)
# ---------------------------------------------------------------------------
SEEDED="$(psql "$SENTINEL_MOCK_DSN" -tAc 'SELECT count(*) FROM sentinel_incident' | tr -d '[:space:]')"
if [[ "$SEEDED" != "1000" ]]; then
  log "Seeding 1000 mock Sentinel incidents"
  set -a; source .env; set +a
  PYTHONPATH="$REPO_ROOT" ./.venv/bin/python -m mock.seed_sentinel
else
  log "Mock Sentinel already seeded (1000 incidents)"
fi

log "Install complete."
