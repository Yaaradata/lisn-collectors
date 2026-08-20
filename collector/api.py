"""Request API that LiSN calls.

These counts come from OUR table, never from procrastinate_jobs. Reading a
library's internal schema would pin LiSN's API to a version we do not control.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from collector.app import app as procrastinate_app
from collector.db import connect
from collector.sources import get
from collector.tasks import fetch_page

api = FastAPI(title="LiSN Collectors Request API")


class CollectBody(BaseModel):
    source: str
    query_spec: dict[str, Any] = Field(default_factory=dict)


def _page_key_count(payload: dict[str, Any]) -> int:
    for field in ("incident_ids", "order_ids"):
        if field in payload and isinstance(payload[field], list):
            return len(payload[field])
    return 0


@api.post("/v1/collect")
def collect(body: CollectBody) -> dict[str, Any]:
    try:
        src = get(body.source)
        pages = src.plan(body.query_spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # plan() runs HERE, once, at request time. It is never called again.
    # Recovery re-reads the stored page_payload rather than recomputing, so a
    # changed underlying dataset can never shift the page boundaries mid-request.

    request_id = uuid.uuid4()
    job_ids: list[uuid.UUID] = []

    # Rows are written BEFORE jobs are deferred. If the process dies between the
    # two, the sweeper finds orphan rows — the safe direction. Deferring first
    # would queue jobs pointing at rows that do not exist.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_request (
                  request_id, source, query_spec, total_pages, status
                )
                VALUES (%s, %s, %s::jsonb, %s, 'open')
                """,
                (
                    request_id,
                    body.source,
                    json.dumps(body.query_spec),
                    len(pages),
                ),
            )
            for page in pages:
                job_id = uuid.uuid4()
                job_ids.append(job_id)
                cur.execute(
                    """
                    INSERT INTO collector_job (
                      job_id, request_id, source, page_no, page_payload, status
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, 'pending')
                    """,
                    (
                        job_id,
                        request_id,
                        body.source,
                        page.page_no,
                        json.dumps(page.payload),
                    ),
                )
        conn.commit()

    with procrastinate_app.open():
        for job_id in job_ids:
            fetch_page.defer(job_id=str(job_id))

    total_keys = sum(_page_key_count(page.payload) for page in pages)
    return {
        "request_id": str(request_id),
        "total_pages": len(pages),
        "keys": total_keys,
    }


@api.get("/v1/requests/{request_id}/counts")
def request_counts(request_id: str) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, count(*)::int
                FROM collector_job
                WHERE request_id = %s::uuid
                GROUP BY status
                """,
                (request_id,),
            )
            counts = {status: count for status, count in cur.fetchall()}
    return {"request_id": request_id, "counts": counts}


@api.get("/v1/counts")
def global_counts() -> dict[str, Any]:
    # This is why all collectors share one collector_job table — LiSN asking
    # for open/in-progress/closed across every source must be one query, not six.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, status, count(*)::int
                FROM collector_job
                GROUP BY source, status
                ORDER BY source, status
                """
            )
            counts: dict[str, dict[str, int]] = {}
            for source, status, count in cur.fetchall():
                counts.setdefault(source, {})[status] = count
    return {"counts": counts}


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
