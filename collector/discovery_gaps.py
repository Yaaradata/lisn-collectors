"""Discovery window ledger and gap detection.

Gaps are reported, never auto-backfilled. Backfill is a LiSN scheduling decision.

Completion of a discovery_window row is detected two ways (see
maybe_finalize_window):
  (a) the discovery task finalises the row after a page reaches a terminal
      status — immediate; preferred for a control tower watching the ledger;
  (b) the sweeper also calls reconcile_running_windows() — backstop so a
      crash between page-done and window-update cannot leave status='running'
      forever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from collector.db import connect
from collector.logging_setup import get_logger, log

logger = get_logger(__name__)

# Keep in sync with sql/011_discovery_gaps.sql (also loaded from disk when present;
# embedded so the Cloud Run image — which copies only collector/ — still starts).
#
# Failed and running windows are deliberately excluded — a failed window IS a
# gap and will show as one (LEAD jumps from the prior complete to the next),
# which is the intent.
_GAPS_SQL_EMBEDDED = """
WITH ordered AS (
  SELECT
    source,
    window_field,
    window_from,
    window_to,
    request_id,
    LEAD(window_from) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_from,
    LEAD(request_id) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_request_id
  FROM discovery_window
  WHERE status = 'complete'
)
SELECT
  source,
  window_field,
  window_to AS gap_from,
  next_from AS gap_to,
  next_from - window_to AS gap_duration,
  request_id AS before_request_id,
  next_request_id AS after_request_id
FROM ordered
WHERE next_from IS NOT NULL
  AND window_to < next_from
ORDER BY source, window_field, gap_from
"""


def _gaps_sql() -> str:
    path = Path(__file__).resolve().parent.parent / "sql" / "011_discovery_gaps.sql"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return _GAPS_SQL_EMBEDDED


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class WindowAxis:
    field: str  # 'updated_on' | 'created_at'
    window_from: datetime
    window_to: datetime


def axes_from_query_spec(query_spec: dict[str, Any]) -> list[WindowAxis]:
    """Extract updated_on / created_at windows present on a discovery query_spec."""
    out: list[WindowAxis] = []
    for field, start_key, end_key in (
        ("updated_on", "updated_from", "updated_to"),
        ("created_at", "created_from", "created_to"),
    ):
        start = query_spec.get(start_key)
        end = query_spec.get(end_key)
        if start is None and end is None:
            continue
        if start is None or end is None:
            continue
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        start_dt = _ensure_aware(start)
        end_dt = _ensure_aware(end)
        if not start_dt < end_dt:
            raise ValueError(
                f"{start_key} must be strictly before {end_key} "
                f"(got {start_dt.isoformat()} .. {end_dt.isoformat()})"
            )
        out.append(
            WindowAxis(
                field=field,
                window_from=start_dt,
                window_to=end_dt,
            )
        )
    return out


@dataclass(frozen=True)
class GapCheck:
    """Result of submit-time contiguity check for one axis."""

    field: str
    gap_from: datetime | None = None
    gap_to: datetime | None = None
    gap_duration: timedelta | None = None
    overlap_with_to: datetime | None = None  # prior window_to if overlapping


def check_submit_contiguity(
    *,
    source: str,
    axis: WindowAxis,
) -> GapCheck:
    """Compare a proposed window to the latest *completed* window on that axis.

    First-ever window (no prior complete) → no gap, not an error.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT window_to
                FROM discovery_window
                WHERE source = %s
                  AND window_field = %s
                  AND status = 'complete'
                ORDER BY window_to DESC
                LIMIT 1
                """,
                (source, axis.field),
            )
            row = cur.fetchone()
    if row is None:
        return GapCheck(field=axis.field)

    latest_to = _ensure_aware(row[0])
    proposed_from = axis.window_from
    if proposed_from > latest_to:
        return GapCheck(
            field=axis.field,
            gap_from=latest_to,
            gap_to=proposed_from,
            gap_duration=proposed_from - latest_to,
        )
    if proposed_from < latest_to:
        # Starts before the last completed window ended — overlap, not a gap.
        return GapCheck(field=axis.field, overlap_with_to=latest_to)
    return GapCheck(field=axis.field)


def insert_discovery_windows(
    *,
    request_id: UUID | str,
    source: str,
    query_spec: dict[str, Any],
    allow_gap: bool = False,
    gap_reason: str | None = None,
) -> None:
    axes = axes_from_query_spec(query_spec)
    if not axes:
        return
    with connect() as conn:
        with conn.cursor() as cur:
            for axis in axes:
                cur.execute(
                    """
                    INSERT INTO discovery_window (
                      source, window_field, window_from, window_to,
                      request_id, status, allow_gap, gap_reason
                    )
                    VALUES (%s, %s, %s, %s, %s::uuid, 'running', %s, %s)
                    """,
                    (
                        source,
                        axis.field,
                        axis.window_from,
                        axis.window_to,
                        str(request_id),
                        allow_gap,
                        gap_reason,
                    ),
                )
        conn.commit()


def maybe_finalize_window(request_id: str) -> str | None:
    """Finalise discovery_window when all pages for the request are terminal.

    Returns the new status ('complete' / 'failed') or None if still running.

    HOW completion is detected — trade-off, recorded here on purpose:
      (a) the discovery task calls this after each page reaches done/dead —
          immediate; preferred for a control tower watching the ledger;
      (b) the sweeper also calls reconcile_running_windows() — backstop so a
          crash between page-done and this UPDATE cannot leave status='running'
          forever.
    A window with any dead page ends at 'failed' (a failed window IS a gap).
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  count(*)::int,
                  count(*) FILTER (WHERE status = 'done')::int,
                  count(*) FILTER (WHERE status = 'dead')::int,
                  coalesce(
                    sum(record_count) FILTER (WHERE status = 'done'), 0
                  )::int
                FROM collector_job
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            total, done, dead, id_count = cur.fetchone()
            if total == 0:
                return None
            if done + dead < total:
                return None  # still in flight
            if dead > 0:
                cur.execute(
                    """
                    UPDATE discovery_window
                    SET status = 'failed',
                        completed_at = now(),
                        id_count = %s
                    WHERE request_id = %s::uuid
                      AND status = 'running'
                    """,
                    (id_count, request_id),
                )
                updated = cur.rowcount
                conn.commit()
                if updated:
                    log(
                        logger,
                        logging.INFO,
                        "discovery window completed",
                        request_id=str(request_id),
                        source="sentinel_discovery",
                        status="failed",
                        record_count=id_count,
                    )
                    return "failed"
                return None
            cur.execute(
                """
                UPDATE discovery_window
                SET status = 'complete',
                    completed_at = now(),
                    id_count = %s
                WHERE request_id = %s::uuid
                  AND status = 'running'
                """,
                (id_count, request_id),
            )
            updated = cur.rowcount
            conn.commit()
            if updated:
                log(
                    logger,
                    logging.INFO,
                    "discovery window completed",
                    request_id=str(request_id),
                    source="sentinel_discovery",
                    status="complete",
                    record_count=id_count,
                )
                return "complete"
            return None


def mark_window_complete(request_id: str) -> None:
    """Compatibility wrapper — prefer maybe_finalize_window."""
    maybe_finalize_window(request_id)


def mark_window_failed(request_id: str) -> None:
    """Force-fail a running window (e.g. sweeper dead-lettered its pages)."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE discovery_window
                SET status = 'failed',
                    completed_at = now(),
                    id_count = COALESCE(
                      id_count,
                      (
                        SELECT coalesce(sum(record_count), 0)::int
                        FROM collector_job
                        WHERE request_id = %s::uuid AND status = 'done'
                      )
                    )
                WHERE request_id = %s::uuid
                  AND status = 'running'
                """,
                (request_id, request_id),
            )
        conn.commit()


def reconcile_running_windows() -> int:
    """Sweeper backstop: finalise running windows whose pages are all terminal."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_id::text
                FROM discovery_window
                WHERE status = 'running'
                """
            )
            running = [row[0] for row in cur.fetchall()]
    finalised = 0
    for rid in running:
        if maybe_finalize_window(rid) is not None:
            finalised += 1
    return finalised


def list_gaps(
    *,
    source: str | None = None,
    range_from: datetime | None = None,
    range_to: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run sql/011_discovery_gaps.sql with optional filters."""
    sql = _gaps_sql().rstrip().rstrip(";")
    # Strip leading comment lines so wrapping in SELECT * FROM (...) stays valid.
    lines = [
        ln
        for ln in sql.splitlines()
        if ln.strip() and not ln.strip().startswith("--")
    ]
    sql = "\n".join(lines).rstrip().rstrip(";")
    clauses: list[str] = []
    params: list[Any] = []
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    if range_from is not None:
        clauses.append("gap_to > %s")
        params.append(_ensure_aware(range_from))
    if range_to is not None:
        clauses.append("gap_from < %s")
        params.append(_ensure_aware(range_to))
    if clauses:
        sql = f"SELECT * FROM ({sql}) AS gaps WHERE " + " AND ".join(clauses)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            desc = cur.description or ()
            cols: list[str] = []
            for d in desc:
                # psycopg: Column(name=...); pg8000: (tuple, ...)
                name = getattr(d, "name", None)
                if name is None and isinstance(d, (tuple, list)) and d:
                    name = d[0]
                cols.append(str(name))
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(zip(cols, row, strict=True))
        for key in ("gap_from", "gap_to"):
            if item.get(key) is not None:
                item[key] = _ensure_aware(item[key]).isoformat()
        if item.get("gap_duration") is not None:
            dur: timedelta = item["gap_duration"]
            item["gap_duration_seconds"] = dur.total_seconds()
            item["gap_duration"] = str(dur)
        for key in ("before_request_id", "after_request_id"):
            if item.get(key) is not None:
                item[key] = str(item[key])
        out.append(item)
    return out


def gap_summary() -> dict[str, Any]:
    """Count + oldest gap for /v1/health/detail."""
    gaps = list_gaps()
    if not gaps:
        return {"count": 0, "oldest": None}
    oldest = min(gaps, key=lambda g: g["gap_from"])
    return {
        "count": len(gaps),
        "oldest": {
            "source": oldest["source"],
            "window_field": oldest["window_field"],
            "gap_from": oldest["gap_from"],
            "gap_to": oldest["gap_to"],
            "gap_duration": oldest["gap_duration"],
            "gap_duration_seconds": oldest["gap_duration_seconds"],
        },
    }
