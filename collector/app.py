import logging
import os
import sys

# Identity is derived from the source plus a STABLE Cloud Run value, never from
# a hostname or a UUID. With jobs, CLOUD_RUN_TASK_INDEX is deterministic across
# executions, so a restarted task 0 has the same identity as the task 0 that
# died. That is what lets Procrastinate's own recovery find its stranded jobs at
# startup instead of relying entirely on the sweeper.
# Computed before telemetry / other collector imports so resource attributes
# and WORKER_ID agree without a circular import.
task_index = os.environ.get("CLOUD_RUN_TASK_INDEX")  # jobs
instance = os.environ.get("CLOUD_RUN_WORKER_POOL_REVISION")  # pools, if set
source = os.environ.get("COLLECTOR_SOURCE", "local")

if task_index is not None:
    WORKER_ID = f"{source}-task{task_index}"
elif instance:
    WORKER_ID = f"{source}-{instance}"
else:
    WORKER_ID = f"{source}-local"

# OTel before any other collector import so instrumentors patch libraries
# before db/procrastinate pull them in.
from collector.telemetry import init_telemetry

init_telemetry()

import procrastinate
from procrastinate import utils
from procrastinate.psycopg_connector import PsycopgConnector

from collector.db import (
    DB_CONNECT_TIMEOUT_S,
    pool_kwargs,
    wait_for_db,
)
from collector.logging_setup import get_logger, log

logger = get_logger("collector.app")

_DSN = os.environ["COLLECTOR_DSN"]

# One INFO line at process start — not three. Structured fields carry identity
# and the connect budget; Cloud Logging / SigNoz filter on worker_id + source.
log(
    logger,
    logging.INFO,
    "worker starting",
    worker_id=WORKER_ID,
    source=source,
    status="starting",
    duration_ms=int(DB_CONNECT_TIMEOUT_S * 1000),
)


def _should_preflight() -> bool:
    """Preflight blocks until DB is up — workers only, never the API import path.

    The API does ``from collector.app import app``; a 300s SystemExit on a blip
    would take the request surface down. Workers are started as
    ``python -m procrastinate … worker`` or with CLOUD_RUN_TASK_INDEX set.
    """
    flag = os.environ.get("COLLECTOR_DB_PREFLIGHT", "").strip().lower()
    if flag in ("0", "false", "no"):
        return False
    if flag in ("1", "true", "yes"):
        return True
    if os.environ.get("CLOUD_RUN_TASK_INDEX") is not None:
        return True
    return "worker" in sys.argv


if _should_preflight():
    # Do not hand Procrastinate a pool until the DB answers. Without this,
    # open(wait=True) used the hard-coded 30s default and exited on Cloud SQL
    # maintenance (col-maintenance-qmd84). Retries with backoff up to budget.
    wait_for_db(_DSN)


class _ResilientPsycopgConnector(PsycopgConnector):
    """Open the pool with the connect budget, not the 30s open() default.

    Procrastinate calls ``await pool.open(wait=True)`` with no timeout kwarg,
    which defaults to 30 seconds ("pool initialization incomplete after 30.0
    sec"). Even after a successful preflight the DB can flap; keep the same
    budget on open-wait. Mid-run drops are handled by check_connection +
    reconnect_timeout (see collector.db.pool_kwargs).
    """

    async def open_async(self, pool=None):  # type: ignore[no-untyped-def]
        if self._async_pool:
            return
        if self._sync_connector is not None:
            await utils.sync_to_async(self._sync_connector.close)
            self._sync_connector = None
        if pool:
            self._pool_externally_set = True
            self._async_pool = pool
        else:
            self._async_pool = await self._create_pool(self._pool_args)
            assert self._async_pool
            await self._async_pool.open(wait=True, timeout=DB_CONNECT_TIMEOUT_S)


# PsycopgConnector is the async psycopg3 connector and the current default.
# SyncPsycopgConnector exists for purely synchronous callers;
# Psycopg2Connector and AiopgConnector are legacy.
#
# COLLECTOR_DSN has two shapes and both work unchanged:
#   local    postgresql://postgres:PW@127.0.0.1:5432/collector
#   deployed postgresql://postgres:PW@/collector?host=/cloudsql/<CONN>
# The deployed form is what lives in the collector-dsn secret.
app = procrastinate.App(
    connector=_ResilientPsycopgConnector(
        conninfo=_DSN,
        **pool_kwargs(),
    ),
    import_paths=["collector.tasks"],
)
app.WORKER_ID = WORKER_ID
