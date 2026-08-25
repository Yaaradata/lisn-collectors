#!/usr/bin/env bash
# Per-boot startup: bring the local PostgreSQL cluster online and confirm the
# collector/mock databases and schemas are present. Idempotent and fast — heavy
# one-time work (package install, seeding) lives in install.sh.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '[start] %s\n' "$*"; }

PGVER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -1 || true)"
if [[ -z "$PGVER" ]]; then
  echo "[start] ERROR: PostgreSQL not installed; run install.sh first" >&2
  exit 1
fi

if ! sudo pg_lsclusters -h 2>/dev/null | awk -v v="$PGVER" '$1==v && $2=="main"{print $4}' | grep -q online; then
  log "Starting PostgreSQL cluster ${PGVER}/main"
  sudo pg_ctlcluster "$PGVER" main start || sudo pg_ctlcluster "$PGVER" main restart
else
  log "PostgreSQL cluster ${PGVER}/main already online"
fi

for _ in $(seq 1 30); do
  if sudo -u postgres pg_isready -q; then break; fi
  sleep 1
done
sudo -u postgres pg_isready

# Confirm the dev databases exist and carry their schema. This guards a boot
# from a base image that predates install.sh's data directory.
export PGPASSWORD="postgres"
COLLECTOR_DSN="postgresql://postgres:postgres@127.0.0.1:5432/collector"
if psql "$COLLECTOR_DSN" -tAc "SELECT to_regclass('public.collector_job')" 2>/dev/null | grep -q collector_job; then
  log "collector schema present"
else
  log "collector schema missing — running install.sh"
  bash .cursor/install.sh
fi

log "Startup complete."
