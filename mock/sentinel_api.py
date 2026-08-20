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
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from mock.reference import MAX_IDS_PER_CALL

# In-memory fault set for Sprint 4 retry demos. In-memory on purpose so a
# process restart clears injected faults.
_FAULTS: set[str] = set()


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
    %(by_order)s
    AND i.order_id = ANY(%(order_ids)s)
  )
ORDER BY i.id, t.created_at NULLS LAST
"""


class SearchRequest(BaseModel):
    incident_ids: list[str] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)


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
    incident_ids = list(body.incident_ids or [])
    order_ids = list(body.order_ids or [])

    # Queries must be per-key. A generic query is not allowed, and this
    # rejection is what stops that rule being bypassed by accident.
    if not incident_ids and not order_ids:
        raise HTTPException(
            status_code=400,
            detail="incident_ids or order_ids required",
        )

    supplied = len(incident_ids) + len(order_ids)
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
    requested = set(incident_ids) | set(order_ids)
    if requested & _FAULTS:
        raise HTTPException(status_code=500, detail="injected fault")

    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                SEARCH_SQL,
                {
                    "by_incident": bool(incident_ids),
                    "incident_ids": incident_ids or [""],
                    "by_order": bool(order_ids),
                    "order_ids": order_ids or [""],
                },
            )
            rows = cur.fetchall()

    incidents = [_row_to_export(row) for row in rows]
    return {"incidents": incidents, "count": len(incidents)}


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
