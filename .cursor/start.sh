#!/usr/bin/env bash
# Per-boot startup for the LiSN collectors dev stack: bring PostgreSQL up.
# The mock Sentinel, request API, and Procrastinate worker run as `terminals`.
set -euo pipefail

PG_VERSION=16

echo "[start] Starting PostgreSQL cluster ${PG_VERSION}/main"
sudo pg_ctlcluster "$PG_VERSION" main start 2>/dev/null || sudo service postgresql start || true

for _ in $(seq 1 30); do
  if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    echo "[start] PostgreSQL is ready on 127.0.0.1:5432"
    exit 0
  fi
  sleep 1
done

echo "[start] PostgreSQL did not become ready in time" >&2
exit 1
