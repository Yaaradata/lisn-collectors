"""Cloud SQL clients — SELECT only.

IMPORTANT — Postgres role:
  Point COLLECTOR_DSN_READONLY / SENTINEL_MOCK_DSN_READONLY at `lisn_agent_ro`
  (CONNECT + USAGE + SELECT only). That role is the structural guarantee a bug
  in the agent cannot write collector state. Session writes use a separate
  `lisn_agent_session` DSN (AGENT_DSN) limited to agent_* tables.
  This code path never issues INSERT/UPDATE/DELETE/TRUNCATE/DROP.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.clients import HealthResult

logger = logging.getLogger(__name__)

# Word-boundary match only — substring "update" must not reject column
# names like updated_on / updated_at (real Sentinel field names).
_FORBIDDEN_RE = re.compile(
    r"\b("
    r"insert|update|delete|truncate|drop|alter|create|grant|revoke|"
    r"copy|call|vacuum|reindex|cluster|refresh|do"
    r")\b",
    re.IGNORECASE,
)


def _assert_readonly_sql(sql: str) -> None:
    lowered = " ".join(sql.lower().split())
    match = _FORBIDDEN_RE.search(lowered)
    if match:
        raise PermissionError(
            f"refusing non-SELECT SQL containing {match.group(0)!r}: "
            "agent is read-only"
        )
    if not lowered.lstrip().startswith(("select", "with", "show", "explain")):
        raise PermissionError(
            "refusing SQL that does not start with SELECT/WITH/SHOW/EXPLAIN"
        )


class SqlClient:
    """Two read-only pools: collector ledger + Sentinel mock source."""

    def __init__(
        self,
        collector_dsn: str,
        sentinel_mock_dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
    ) -> None:
        kwargs: dict[str, Any] = {
            "min_size": min_size,
            "max_size": max_size,
            "kwargs": {"row_factory": dict_row, "autocommit": True},
            "open": False,
        }
        self._collector = ConnectionPool(conninfo=collector_dsn, **kwargs)
        self._sentinel = ConnectionPool(conninfo=sentinel_mock_dsn, **kwargs)
        self._collector.open()
        self._sentinel.open()

    def close(self) -> None:
        self._collector.close()
        self._sentinel.close()

    def fetch_collector(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        _assert_readonly_sql(sql)
        with self._collector.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def fetch_sentinel_mock(
        self, sql: str, params: Any = None
    ) -> list[dict[str, Any]]:
        _assert_readonly_sql(sql)
        with self._sentinel.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())

    def health_check(self) -> HealthResult:
        try:
            collector_role = self._probe(self._collector, "collector")
            mock_role = self._probe(self._sentinel, "sentinel_mock")
            if collector_role == "lisn_agent_ro" and mock_role == "lisn_agent_ro":
                note = "SELECT-only role lisn_agent_ro (structural write block)."
            else:
                note = (
                    f"WARNING: expected lisn_agent_ro, got "
                    f"collector={collector_role} mock={mock_role}."
                )
            return HealthResult(
                name="sql",
                status="ok",
                message=(
                    f"collector ok (role={collector_role}); "
                    f"sentinel_mock ok (role={mock_role}). {note}"
                ),
            )
        except Exception as exc:  # noqa: BLE001 — surface to /health/sources
            logger.exception("sql health_check failed")
            return HealthResult(name="sql", status="error", message=str(exc))

    @staticmethod
    def _probe(pool: ConnectionPool, label: str) -> str:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_user AS role, current_database() AS db")
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(f"{label}: empty probe result")
                return str(row["role"])
