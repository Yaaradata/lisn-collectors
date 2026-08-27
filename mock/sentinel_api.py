"""Fake Flipkart Sentinel export service (FastAPI).

The real Sentinel is a web console with a Download button, not an API. We model
it as a REST endpoint because that is the cleanest thing to demo, and because in
our collector design only the fetch() method differs between an API and a file
download. This mock is a deliberate simplification — not an assumption that
Sentinel has a public HTTP API.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from mock.reference import MAX_IDS_PER_CALL

# In-memory fault set for Sprint 4 retry demos. In-memory on purpose so a
# process restart clears injected faults.
_FAULTS: set[str] = set()
_PAYLOAD_FAULTS: dict[str, str] = {}

# Request counter for Sprint 5 rate measurement (scripts/30_measure_rate.sh).
# In-memory on purpose — one mock instance; a rolling deploy briefly doubles it.
_REQUESTS: int = 0

# Real Sentinel console caps Create/Update date ranges at 15 days.
MAX_DISCOVER_WINDOW = timedelta(days=15)
DISCOVER_LIMIT_DEFAULT = 1000
DISCOVER_LIMIT_MAX = 5000


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'").strip('"')


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_to_export(row: dict[str, Any]) -> dict[str, Any]:
    """Re-dot snake_case DB columns to real Sentinel export field names."""
    return {
        "id": row["id"],
        "issue.id": row["issue_id"],
        "issue.name": row["issue_name"],
        "issue.parentResponse.id": row["issue_parent_id"],
        "issue.parentResponse.name": row["issue_parent_name"],
        "orderId": row["order_id"],
        "orderItemId": row["order_item_id"],
        "orderItemUnitId": row["order_item_unit_id"],
        "trackingId": row["tracking_id"],
        "orderItemProductFSN": row["order_item_product_fsn"],
        "incidentScore": row["incident_score"],
        "resolutionDeadline": _iso(row["resolution_deadline"]),
        "resolutionDeadlineBreach": row["resolution_deadline_breach"],
        "sellerId": row["seller_id"],
        "source": row["source"],
        "status.id": row["status_id"],
        "status.status": row["status_status"],
        "status.statusType": row["status_status_type"],
        "subject": row["subject"],
        "updatedOn": _iso(row["updated_on"]),
        "agingScore": row["aging_score"],
        "lastUpdatedByUser": row["last_updated_by_user"],
        "queue": row["queue"],
        "assignedTo": row["assigned_to"],
        "threads.id": row["thread_id"],
        "threads.channel.id": row["channel_id"],
        "threads.channel.name": row["channel_name"],
        "threads.communicationId": row["communication_id"],
        "threads.contentType": row["content_type"],
        "threads.createdAt": _iso(row["thread_created_at"]),
        "threads.createdBy": row["created_by"],
        "threads.systemThread": row["system_thread"],
        "threads.threadEntryType.id": row["thread_entry_type_id"],
        "threads.threadEntryType.name": row["thread_entry_type_name"],
        "threads.updatedBy": row["updated_by"],
    }


SEARCH_SQL = """
SELECT
  i.id,
  i.issue_id,
  i.issue_name,
  i.issue_parent_id,
  i.issue_parent_name,
  i.order_id,
  i.order_item_id,
  i.order_item_unit_id,
  i.tracking_id,
  i.order_item_product_fsn,
  i.incident_score,
  i.resolution_deadline,
  i.resolution_deadline_breach,
  i.seller_id,
  i.source,
  i.status_id,
  i.status_status,
  i.status_status_type,
  i.subject,
  i.updated_on,
  i.aging_score,
  i.last_updated_by_user,
  i.queue,
  i.assigned_to,
  t.thread_id,
  t.channel_id,
  t.channel_name,
  t.communication_id,
  t.content_type,
  t.created_at AS thread_created_at,
  t.created_by,
  t.system_thread,
  t.thread_entry_type_id,
  t.thread_entry_type_name,
  t.updated_by
FROM sentinel_incident AS i
LEFT JOIN sentinel_thread AS t
  ON t.incident_id = i.id
WHERE
  (
    %(by_incident)s
    AND i.id = ANY(%(incident_ids)s)
  )
  OR (
    %(by_order_item)s
    AND i.order_item_id = ANY(%(order_item_ids)s)
  )
  OR (
    %(by_order)s
    AND i.order_id = ANY(%(order_ids)s)
  )
ORDER BY i.id, t.created_at NULLS LAST
"""


class SearchRequest(BaseModel):
    incident_ids: list[str] = Field(default_factory=list)
    # JSON may send ints, floats, or digit-strings; converted to numeric below.
    order_item_ids: list[Any] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)


class DiscoverRequest(BaseModel):
    """Filters matching the real Sentinel console Download screen."""

    updated_from: datetime | None = None
    updated_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    statuses: list[str] = Field(default_factory=list)
    issue_names: list[str] = Field(default_factory=list)
    cursor: str | None = None
    limit: int = DISCOVER_LIMIT_DEFAULT


_VALID_PAYLOAD_FAULT_MODES = {
    "truncated_json",
    "html_error_page",
    "empty_body_200",
    "incidents_string",
}


def _as_order_item_id(value: Any) -> float:
    # order_item_id is numeric in sentinel_incident (see sql/002_sentinel_mock.sql).
    # Convert JSON body values explicitly rather than relying on implicit casting
    # in PostgreSQL / psycopg — callers may send int, float, or string.
    if isinstance(value, bool):
        raise HTTPException(
            status_code=400,
            detail=f"order_item_id must be numeric, got bool {value!r}",
        )
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"order_item_id must be numeric, got {value!r}",
            ) from exc
    raise HTTPException(
        status_code=400,
        detail=f"order_item_id must be numeric, got {type(value).__name__}",
    )


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _validate_discover_window(
    name: str, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime] | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise HTTPException(
            status_code=400,
            detail=f"{name}_from and {name}_to must both be set for a {name} window",
        )
    start_a = _ensure_aware(start)
    end_a = _ensure_aware(end)
    if end_a < start_a:
        raise HTTPException(
            status_code=400,
            detail=f"{name} window end must be on or after start",
        )
    # The real Sentinel console caps date ranges at 15 days. We enforce it so
    # that a caller assuming otherwise fails loudly here rather than silently
    # getting truncated results from the real system later.
    if end_a - start_a > MAX_DISCOVER_WINDOW:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{name} window exceeds the 15-day limit "
                f"(got {(end_a - start_a).days} days)"
            ),
        )
    return start_a, end_a


# Discovery answers "which" (ids only). Enrichment (/search) answers "what".
# Returning bodies here would duplicate the enrichment path with none of its
# rate control or paging.
#
# Keyset on id (WHERE id > cursor), not OFFSET: OFFSET paging over a moving
# dataset skips and repeats rows. Discovery runs against a live system where
# incidents are being created and updated while we page.
DISCOVER_SQL = """
SELECT i.id
FROM sentinel_incident AS i
WHERE
  (
    NOT %(by_updated)s
    OR (i.updated_on >= %(updated_from)s AND i.updated_on <= %(updated_to)s)
  )
  AND (
    NOT %(by_created)s
    OR (i.created_at >= %(created_from)s AND i.created_at <= %(created_to)s)
  )
  AND (
    NOT %(by_status)s
    OR i.status_status = ANY(%(statuses)s)
  )
  AND (
    NOT %(by_issue)s
    OR i.issue_name = ANY(%(issue_names)s)
  )
  AND (
    NOT %(by_cursor)s
    OR i.id > %(cursor)s
  )
ORDER BY i.id
LIMIT %(fetch_limit)s
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_dotenv()
    dsn = os.environ.get("SENTINEL_MOCK_DSN")
    if not dsn:
        raise RuntimeError("SENTINEL_MOCK_DSN is required")
    pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=10, open=True)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Mock Sentinel Export", lifespan=lifespan)


@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sentinel_incident")
            (n,) = cur.fetchone()
    return {"status": "ok", "incidents": n}


@app.post("/v1/incidents/search")
def search_incidents(body: SearchRequest, request: Request) -> dict[str, Any]:
    global _REQUESTS
    _REQUESTS += 1

    incident_ids = list(body.incident_ids or [])
    # Explicit numeric conversion — see _as_order_item_id.
    order_item_ids = [_as_order_item_id(v) for v in (body.order_item_ids or [])]
    order_ids = list(body.order_ids or [])

    # Queries must be per-key. A generic query is not allowed, and this
    # rejection is what stops that rule being bypassed by accident.
    if not incident_ids and not order_item_ids and not order_ids:
        raise HTTPException(
            status_code=400,
            detail="incident_ids, order_item_ids or order_ids required",
        )

    supplied = len(incident_ids) + len(order_item_ids) + len(order_ids)
    # Multi Track states this limit on its own screen. If the collector's
    # paging logic ever breaks, this makes it fail loudly instead of silently
    # truncating.
    if supplied > MAX_IDS_PER_CALL:
        raise HTTPException(
            status_code=400,
            detail=f"max 50 ids per call, got {supplied}",
        )

    # Exists so we can demonstrate retry with backoff on demand. In-memory on
    # purpose so a restart clears it.
    requested = set(incident_ids) | {str(x) for x in order_item_ids} | set(order_ids)
    if requested & _FAULTS:
        raise HTTPException(status_code=500, detail="injected fault")
    mode: str | None = None
    for ident in requested:
        if ident in _PAYLOAD_FAULTS:
            mode = _PAYLOAD_FAULTS[ident]
            break
    if mode == "truncated_json":
        return Response(
            content='{"incidents":[',
            media_type="application/json",
            status_code=200,
        )
    if mode == "html_error_page":
        return Response(
            content="<html><body><h1>500 upstream</h1></body></html>",
            media_type="text/html",
            status_code=500,
        )
    if mode == "empty_body_200":
        return Response(
            content="",
            media_type="application/json",
            status_code=200,
        )
    if mode == "incidents_string":
        return {
            "incidents": "not-a-list",
            "count": 11,
        }

    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                SEARCH_SQL,
                {
                    "by_incident": bool(incident_ids),
                    "incident_ids": incident_ids or [""],
                    "by_order_item": bool(order_item_ids),
                    # Placeholder when unused: numeric ANY needs a typed empty-safe value.
                    "order_item_ids": order_item_ids or [-1.0],
                    "by_order": bool(order_ids),
                    "order_ids": order_ids or [""],
                },
            )
            rows = cur.fetchall()

    incidents = [_row_to_export(row) for row in rows]
    return {"incidents": incidents, "count": len(incidents)}


@app.post("/v1/incidents/discover")
def discover_incidents(body: DiscoverRequest, request: Request) -> dict[str, Any]:
    """Console-shaped discovery: which incident ids match filters + time window."""
    global _REQUESTS
    _REQUESTS += 1

    updated = _validate_discover_window(
        "updated", body.updated_from, body.updated_to
    )
    created = _validate_discover_window(
        "created", body.created_from, body.created_to
    )

    # Discovery must be bounded in time. An unbounded discovery over the whole
    # incident history is exactly the unbounded query the per-key rule exists
    # to prevent.
    if updated is None and created is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "at least one time window required "
                "(updated_from/updated_to or created_from/created_to)"
            ),
        )

    limit = body.limit
    if limit < 1 or limit > DISCOVER_LIMIT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {DISCOVER_LIMIT_MAX}, got {limit}",
        )

    statuses = list(body.statuses or [])
    issue_names = list(body.issue_names or [])
    cursor = body.cursor

    # Placeholder timestamps when a window is unused (predicate gated off).
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    updated_from, updated_to = updated if updated else (epoch, epoch)
    created_from, created_to = created if created else (epoch, epoch)

    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                DISCOVER_SQL,
                {
                    "by_updated": updated is not None,
                    "updated_from": updated_from,
                    "updated_to": updated_to,
                    "by_created": created is not None,
                    "created_from": created_from,
                    "created_to": created_to,
                    "by_status": bool(statuses),
                    "statuses": statuses or [""],
                    "by_issue": bool(issue_names),
                    "issue_names": issue_names or [""],
                    "by_cursor": bool(cursor),
                    "cursor": cursor or "",
                    # Fetch one extra to detect has_more without a second count query.
                    "fetch_limit": limit + 1,
                },
            )
            rows = [r[0] for r in cur.fetchall()]

    has_more = len(rows) > limit
    incident_ids = rows[:limit]
    next_cursor = incident_ids[-1] if has_more and incident_ids else None

    return {
        "incident_ids": incident_ids,
        "count": len(incident_ids),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@app.post("/admin/fault/{ident}")
def add_fault(ident: str) -> dict[str, Any]:
    # Exists so we can demonstrate retry with backoff on demand. In-memory on
    # purpose so a restart clears it.
    _FAULTS.add(ident)
    return {"faults": sorted(_FAULTS)}


@app.delete("/admin/fault")
def clear_faults() -> dict[str, Any]:
    _FAULTS.clear()
    return {"faults": []}


@app.get("/admin/fault")
def list_faults() -> dict[str, Any]:
    return {"faults": sorted(_FAULTS)}


@app.get("/admin/null-thread-ids")
def list_null_thread_ids(request: Request, limit: int = 200) -> dict[str, Any]:
    if limit < 1 or limit > 5000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id
                FROM sentinel_incident AS i
                LEFT JOIN sentinel_thread AS t
                  ON t.incident_id = i.id
                WHERE t.thread_id IS NULL
                ORDER BY i.id
                LIMIT %s
                """,
                (limit,),
            )
            ids = [str(row[0]) for row in cur.fetchall()]
    return {"incident_ids": ids, "count": len(ids), "limit": limit}


@app.post("/admin/seed-acceptance-probes")
def seed_acceptance_probes(request: Request) -> dict[str, Any]:
    """Seed precision/null-thread probe incidents for deployed acceptance checks."""
    now = datetime.now(timezone.utc)
    precision_rows = [
        ("IN270827PRECISION01", 9007199254740991),
        ("IN270827PRECISION02", 9007199254740993),
        ("IN270827PRECISION03", 1234567890123456789),
    ]
    null_thread_id = "IN270827NULLTHREAD0001"

    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for idx, (incident_id, order_item_id) in enumerate(precision_rows, start=1):
                cur.execute("DELETE FROM sentinel_thread WHERE incident_id = %s", (incident_id,))
                cur.execute("DELETE FROM sentinel_incident WHERE id = %s", (incident_id,))
                cur.execute(
                    """
                    INSERT INTO sentinel_incident (
                      id, issue_id, issue_name, order_id, order_item_id, order_item_unit_id,
                      order_item_product_fsn, source, status_id, status_status, status_status_type,
                      subject, updated_on, created_at, queue, assigned_to
                    ) VALUES (
                      %s, 3267, 'Delay in Delivery', %s, %s, %s,
                      %s, 'Sentinel', 1, 'Unresolved', 'UNRESOLVED',
                      'Precision probe', %s, %s, 'IMS V2', 'system'
                    )
                    """,
                    (
                        incident_id,
                        f"ODPREC{idx:06d}",
                        str(order_item_id),
                        str(order_item_id + 1000),
                        f"FSNPREC{idx:06d}",
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO sentinel_thread (
                      thread_id, incident_id, channel_id, channel_name, communication_id,
                      content_type, created_at, created_by, system_thread,
                      thread_entry_type_id, thread_entry_type_name, updated_at, updated_by
                    ) VALUES (
                      %s, %s, 5, 'Outbound', %s,
                      'text/plain', %s, 'system', true,
                      1005, 'Proactive', %s, 'system'
                    )
                    """,
                    (
                        f"THPREC{idx:06d}",
                        incident_id,
                        str(order_item_id + 2000),
                        now,
                        now,
                    ),
                )

            cur.execute("DELETE FROM sentinel_thread WHERE incident_id = %s", (null_thread_id,))
            cur.execute("DELETE FROM sentinel_incident WHERE id = %s", (null_thread_id,))
            cur.execute(
                """
                INSERT INTO sentinel_incident (
                  id, issue_id, issue_name, order_id, order_item_id, order_item_unit_id,
                  order_item_product_fsn, source, status_id, status_status, status_status_type,
                  subject, updated_on, created_at, queue, assigned_to
                ) VALUES (
                  %s, 7570, 'Request for Reschedule Delivery', %s, %s, %s,
                  %s, 'Sentinel', 1, 'Unresolved', 'UNRESOLVED',
                  'Null-thread probe', %s, %s, 'IMS V2', 'system'
                )
                """,
                (
                    null_thread_id,
                    "ODNULL000001",
                    "4000000000999999",
                    "5000000000999999",
                    "FSNNULL000001",
                    now,
                    now,
                ),
            )
        conn.commit()

    return {
        "precision_incident_ids": [incident_id for incident_id, _ in precision_rows],
        "precision_values": [str(value) for _, value in precision_rows],
        "null_thread_incident_id": null_thread_id,
        "seeded_at": now.isoformat(),
    }


@app.post("/admin/payload-fault/{ident}/{mode}")
def add_payload_fault(ident: str, mode: str) -> dict[str, Any]:
    if mode not in _VALID_PAYLOAD_FAULT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode must be one of {sorted(_VALID_PAYLOAD_FAULT_MODES)}",
        )
    _PAYLOAD_FAULTS[ident] = mode
    return {"payload_faults": dict(sorted(_PAYLOAD_FAULTS.items()))}


@app.delete("/admin/payload-fault")
def clear_payload_faults() -> dict[str, Any]:
    _PAYLOAD_FAULTS.clear()
    return {"payload_faults": {}}


@app.get("/admin/payload-fault")
def list_payload_faults() -> dict[str, Any]:
    return {"payload_faults": dict(sorted(_PAYLOAD_FAULTS.items()))}


@app.get("/admin/stats")
def get_stats() -> dict[str, Any]:
    return {"requests": _REQUESTS}


@app.delete("/admin/stats")
def reset_stats() -> dict[str, Any]:
    global _REQUESTS
    _REQUESTS = 0
    return {"requests": _REQUESTS}
