"""psycopg helpers for collector state tables.

Connection setup retries with exponential backoff so a Cloud SQL restart
(typically 1–3 minutes) does not kill the worker. Budget is configurable via
DB_CONNECT_TIMEOUT_S (default 300).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

import psycopg

from collector.logging_setup import get_logger, log
from collector.redact import redact_secrets

logger = get_logger(__name__)

# Cloud SQL maintenance restarts commonly take 1–3 minutes. The previous
# implicit ceiling was psycopg_pool's open(wait=True) default of 30s — below
# that floor, so every maintenance event killed the worker.
DB_CONNECT_TIMEOUT_S = float(os.environ.get("DB_CONNECT_TIMEOUT_S", "300"))

# Per-attempt libpq connect_timeout (seconds). Keep short so the outer retry
# loop can log and back off rather than appearing hung on one TCP attempt.
_ATTEMPT_CONNECT_TIMEOUT_S = 5

# Current psycopg_pool defaults Procrastinate inherited BEFORE this change
# (reported here so a reviewer can see what moved):
#   timeout=30.0            ← getconn wait; ALSO open(wait=True) default 30s
#   reconnect_timeout=300.0 ← already 5 min for mid-run background reconnect
#   check=check_connection  ← already set by Procrastinate's _create_pool
#   min_size=4, max_idle=600, max_lifetime=3600
# We raise pool timeout / open-wait to DB_CONNECT_TIMEOUT_S and keep
# reconnect_timeout at the same budget. check_connection stays on.


def dsn_for_log(dsn: str | None = None) -> str:
    """Return COLLECTOR_DSN with password redacted (Pass 1 helper)."""
    raw = dsn if dsn is not None else os.environ.get("COLLECTOR_DSN", "")
    return redact_secrets(raw) or ""


def wait_for_db(
    dsn: str | None = None,
    *,
    budget_s: float | None = None,
) -> None:
    """Block until a single connection succeeds, or exit after *budget_s*.

    First-try success is silent at INFO (DEBUG only). Failures log one WARNING
    per attempt with the redacted error. On recovery after retries, one WARNING
    "database reachable after backoff". On exhaustion, one CRITICAL then exit.
    """
    conninfo = dsn if dsn is not None else os.environ["COLLECTOR_DSN"]
    budget = DB_CONNECT_TIMEOUT_S if budget_s is None else float(budget_s)
    started = time.monotonic()
    attempt = 0
    last_exc: BaseException | None = None
    delay = 1.0

    while True:
        attempt += 1
        elapsed = time.monotonic() - started
        if elapsed >= budget and attempt > 1:
            break
        try:
            with psycopg.connect(
                conninfo,
                connect_timeout=_ATTEMPT_CONNECT_TIMEOUT_S,
            ) as conn:
                conn.execute("SELECT 1")
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if attempt > 1:
                log(
                    logger,
                    logging.WARNING,
                    "database reachable after backoff",
                    attempt=attempt,
                    duration_ms=elapsed_ms,
                    status="connected",
                )
            else:
                log(
                    logger,
                    logging.DEBUG,
                    "database connected",
                    attempt=attempt,
                    duration_ms=elapsed_ms,
                    status="connected",
                )
            return
        except Exception as exc:  # noqa: BLE001 — any connect failure retries
            last_exc = exc
            elapsed = time.monotonic() - started
            remaining = budget - elapsed
            log(
                logger,
                logging.WARNING,
                "database connect attempt failed",
                attempt=attempt,
                duration_ms=int(elapsed * 1000),
                status="retry",
                error=redact_secrets(str(exc)),
            )
            if remaining <= 0:
                break
            sleep_for = min(delay, 30.0, remaining)
            if sleep_for < 0.05:
                break
            time.sleep(sleep_for)
            delay = min(delay * 2.0, 30.0)

    elapsed = time.monotonic() - started
    err = redact_secrets(str(last_exc))
    log(
        logger,
        logging.CRITICAL,
        "worker cannot reach the database at startup after full backoff",
        attempt=attempt,
        duration_ms=int(elapsed * 1000),
        status="unreachable",
        error=err,
    )
    # Non-zero exit so Cloud Run marks the task failed rather than hanging.
    print(
        f"database unreachable after {elapsed:.1f}s "
        f"(budget={budget:.0f}s, attempts={attempt}, "
        f"dsn={dsn_for_log(conninfo)}): {err}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(1)


def connect(*, retry: bool = True) -> psycopg.Connection:
    """Return a psycopg connection to COLLECTOR_DSN.

    Procrastinate manages its own pool separately; this helper is only for
    collector_request / collector_job / raw_manifest.

    When *retry* is True (default), a transient outage mid-run is retried with
    the same backoff budget as startup — Cloud SQL maintenance does not only
    happen at process start.
    """
    conninfo = os.environ["COLLECTOR_DSN"]
    if not retry:
        return psycopg.connect(
            conninfo, connect_timeout=_ATTEMPT_CONNECT_TIMEOUT_S
        )

    started = time.monotonic()
    budget = DB_CONNECT_TIMEOUT_S
    attempt = 0
    last_exc: BaseException | None = None
    delay = 1.0
    while True:
        attempt += 1
        elapsed = time.monotonic() - started
        if elapsed >= budget and attempt > 1:
            break
        try:
            return psycopg.connect(
                conninfo, connect_timeout=_ATTEMPT_CONNECT_TIMEOUT_S
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            elapsed = time.monotonic() - started
            remaining = budget - elapsed
            log(
                logger,
                logging.WARNING,
                "database connect attempt failed",
                attempt=attempt,
                duration_ms=int(elapsed * 1000),
                status="retry",
                error=redact_secrets(str(exc)),
            )
            if remaining <= 0:
                break
            sleep_for = min(delay, 30.0, remaining)
            if sleep_for < 0.05:
                break
            time.sleep(sleep_for)
            delay = min(delay * 2.0, 30.0)

    raise psycopg.OperationalError(
        f"database unreachable after {time.monotonic() - started:.1f}s "
        f"(budget={budget:.0f}s, attempts={attempt}): "
        f"{redact_secrets(str(last_exc))}"
    )


def pool_kwargs() -> dict[str, Any]:
    """Kwargs for Procrastinate's PsycopgConnector / AsyncConnectionPool.

    Raises ``timeout`` (getconn wait) to the connect budget. ``reconnect_timeout``
    was already 300s by default — set explicitly so it tracks the env var.
    ``check`` is added by Procrastinate's ``_create_pool`` (check_connection).
    """
    return {
        "timeout": DB_CONNECT_TIMEOUT_S,
        "reconnect_timeout": DB_CONNECT_TIMEOUT_S,
        "reconnect_failed": _reconnect_failed,
    }


def _reconnect_failed(pool: Any) -> None:
    """Log when the pool exhausts one reconnect_timeout cycle mid-run."""
    del pool
    log(
        logger,
        logging.WARNING,
        "psycopg pool reconnect_timeout exhausted; starting new cycle",
        duration_ms=int(DB_CONNECT_TIMEOUT_S * 1000),
        status="reconnect",
    )
