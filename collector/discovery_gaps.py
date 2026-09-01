"""Discovery window ledger and gap detection.

Gaps are reported, never auto-backfilled. Backfill is a LiSN scheduling decision.

Completion of a discovery_window row is detected two ways (see
maybe_finalize_window):
  (a) the discovery task finalises the row after a page reaches a terminal
      status — immediate; preferred for a control tower watching the ledger;
  (b) the sweeper also calls reconcile_running_windows() — backstop so a
      crash between page-done and window-update cannot leave status='running'
      forever.

A window whose id_count hits the effective per-request ID cap (limit × cursor
page cap) is status='partial', not 'complete' — it stopped at the limit, not
the end of the data.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from collector.db import connect
from collector.logging_setup import get_logger, log

logger = get_logger(__name__)

# Keep in sync with collector.sources.sentinel_discovery (batch_cap, CURSOR_PAGE_CAP).
_DISCOVERY_BATCH_CAP_DEFAULT = 1000
_DISCOVERY_CURSOR_PAGE_CAP = 10

# Keep in sync with sql/011_discovery_gaps.sql (also loaded from disk when present;
# embedded so the Cloud Run image — which copies only collector/ — still starts).
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
),
boundary_gaps AS (
  SELECT
    source,
    window_field,
    window_to AS gap_from,
    next_from AS gap_to,
    next_from - window_to AS gap_duration,
    request_id AS before_request_id,
    next_request_id AS after_request_id,
    'not_scheduled' AS reason,
    false AS uncertain
  FROM ordered
  WHERE next_from IS NOT NULL
    AND window_to < next_from
),
truncated_gaps AS (
  SELECT
    source,
    window_field,
    window_from AS gap_from,
    window_to AS gap_to,
    window_to - window_from AS gap_duration,
    request_id AS before_request_id,
    NULL::uuid AS after_request_id,
    'truncated' AS reason,
    true AS uncertain
  FROM discovery_window
  WHERE status = 'partial'
)
SELECT * FROM boundary_gaps
UNION ALL
SELECT * FROM truncated_gaps
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


def effective_discovery_limit(query_spec: dict[str, Any]) -> int:
    """Max distinct IDs one discovery request can return (limit × cursor pages)."""
    limit = query_spec.get("limit", _DISCOVERY_BATCH_CAP_DEFAULT)
    if not isinstance(limit, int) or isinstance(limit, bool):
        limit = _DISCOVERY_BATCH_CAP_DEFAULT
    return int(limit) * _DISCOVERY_CURSOR_PAGE_CAP


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
    Partial windows do not count as coverage — only status='complete'.
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


def estimate_truncation_risk(
    *,
    source: str,
    axis: WindowAxis,
    query_spec: dict[str, Any],
) -> dict[str, Any] | None:
    """Warn when recent throughput suggests this window may hit the ID cap."""
    effective_limit = effective_discovery_limit(query_spec)
    window_hours = (
        axis.window_to - axis.window_from
    ).total_seconds() / 3600.0
    if window_hours <= 0:
        return None

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id_count, window_from, window_to
                FROM discovery_window
                WHERE source = %s
                  AND window_field = %s
                  AND status IN ('complete', 'partial')
                  AND id_count IS NOT NULL
                  AND id_count > 0
                  AND completed_at IS NOT NULL
                ORDER BY completed_at DESC
                LIMIT 20
                """,
                (source, axis.field),
            )
            rows = cur.fetchall()

    rates: list[float] = []
    for id_count, window_from, window_to in rows:
        hours = (
            _ensure_aware(window_to) - _ensure_aware(window_from)
        ).total_seconds() / 3600.0
        if hours > 0:
            rates.append(float(id_count) / hours)

    if not rates:
        return None

    ids_per_hour = sum(rates) / len(rates)
    estimated_ids = ids_per_hour * window_hours
    if estimated_ids <= effective_limit:
        return None

    return {
        "window_field": axis.field,
        "window_hours": round(window_hours, 2),
        "effective_limit": effective_limit,
        "ids_per_hour_estimate": round(ids_per_hour, 2),
        "estimated_ids": int(estimated_ids),
        "truncation_likely": True,
        "hint": (
            "narrow the time window or raise limit — this request may stop at "
            f"{effective_limit} IDs while data remains"
        ),
    }


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


def _load_query_spec(cur, request_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT query_spec
        FROM collector_request
        WHERE request_id = %s::uuid
        """,
        (request_id,),
    )
    row = cur.fetchone()
    if row is None:
        return {}
    spec = row[0]
    if isinstance(spec, str):
        return json.loads(spec)
    if isinstance(spec, dict):
        return spec
    return dict(spec)


def maybe_finalize_window(request_id: str) -> str | None:
    """Finalise discovery_window when all pages for the request are terminal.

    Returns the new status ('complete' / 'partial' / 'failed') or None if still
    running.

    HOW completion is detected — trade-off, recorded here on purpose:
      (a) the discovery task calls this after each page reaches done/dead —
          immediate; preferred for a control tower watching the ledger;
      (b) the sweeper also calls reconcile_running_windows() — backstop so a
          crash between page-done and this UPDATE cannot leave status='running'
          forever.
    A window with any dead page ends at 'failed' (a failed window IS a gap).
    id_count == effective limit → 'partial' (stopped at cap, not end of data).
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

            query_spec = _load_query_spec(cur, request_id)
            effective_limit = effective_discovery_limit(query_spec)
            final_status = (
                "partial" if id_count == effective_limit else "complete"
            )

            cur.execute(
                """
                UPDATE discovery_window
                SET status = %s,
                    completed_at = now(),
                    id_count = %s
                WHERE request_id = %s::uuid
                  AND status = 'running'
                """,
                (final_status, id_count, request_id),
            )
            updated = cur.rowcount
            conn.commit()
            if updated:
                level = (
                    logging.CRITICAL
                    if final_status == "partial"
                    else logging.INFO
                )
                log(
                    logger,
                    level,
                    "discovery window completed",
                    request_id=str(request_id),
                    source="sentinel_discovery",
                    status=final_status,
                    record_count=id_count,
                    effective_limit=effective_limit,
                )
                return final_status
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


def _format_gap_row(item: dict[str, Any]) -> dict[str, Any]:
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
    if "uncertain" in item:
        item["uncertain"] = bool(item["uncertain"])
    return item


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
        item = _format_gap_row(dict(zip(cols, row, strict=True)))
        out.append(item)
    return out


def list_partial_windows(
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """Partial discovery windows — stopped at the ID cap."""
    clauses = ["status = 'partial'"]
    params: list[Any] = []
    if source is not None:
        clauses.append("source = %s")
        params.append(source)
    where = " AND ".join(clauses)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT source, window_field, window_from, window_to,
                       id_count, request_id::text
                FROM discovery_window
                WHERE {where}
                ORDER BY window_from
                """,
                params,
            )
            rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for source_name, field, wf, wt, id_count, rid in rows:
        out.append(
            {
                "source": source_name,
                "window_field": field,
                "window_from": _ensure_aware(wf).isoformat(),
                "window_to": _ensure_aware(wt).isoformat(),
                "id_count": id_count,
                "request_id": rid,
            }
        )
    return out


def partial_windows_summary(
    *,
    source: str | None = None,
) -> dict[str, Any]:
    windows = list_partial_windows(source=source)
    return {"count": len(windows), "windows": windows}


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
            "reason": oldest.get("reason"),
            "uncertain": oldest.get("uncertain"),
        },
    }
