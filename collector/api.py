"""Request API that LiSN calls.

These counts come from OUR table, never from procrastinate_jobs. Reading a
library's internal schema would pin LiSN's API to a version we do not control.

Operational endpoints (/v1/reconcile, /v1/dead-letter, /v1/health/detail) also
read Procrastinate's tables. LiSN's public counts endpoint reads only ours.
These three are operational.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from collector.app import app as procrastinate_app
from collector.db import connect
from collector.sources import get
from collector.tasks import fetch_page

api = FastAPI(title="LiSN Collectors Request API")


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(columns: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    return {col: _jsonable(val) for col, val in zip(columns, row, strict=True)}


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


# WHY reconcile exists, and why review called it non-negotiable: a page can
# succeed at writing raw JSON to GCS and then the process dies before the
# BigQuery insert. That failure produces no error anywhere. Every other failure
# mode is loud; this one is silent. The 15-minute grace period exists so a page
# currently mid-flight is not reported as a gap.
@api.get("/v1/reconcile")
def reconcile(minutes: int = Query(default=15, ge=0)) -> dict[str, Any]:
    columns = (
        "job_id",
        "source",
        "request_id",
        "page_no",
        "raw_uri",
        "raw_written_at",
        "attempts",
    )
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, request_id, page_no, raw_uri,
                       raw_written_at, attempts
                FROM collector_job
                WHERE raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                  AND raw_written_at < now() - make_interval(mins => %s)
                ORDER BY raw_written_at
                """,
                (minutes,),
            )
            rows = [_row_dict(columns, row) for row in cur.fetchall()]
    return {"unloaded": len(rows), "rows": rows}


# These need a human. They exhausted their attempts and nothing will retry
# them automatically.
@api.get("/v1/dead-letter")
def dead_letter() -> dict[str, Any]:
    columns = (
        "job_id",
        "source",
        "request_id",
        "page_no",
        "attempts",
        "last_error",
        "updated_at",
    )
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, request_id, page_no, attempts,
                       last_error, updated_at
                FROM collector_job
                WHERE status = 'dead'
                ORDER BY updated_at
                """
            )
            rows = [_row_dict(columns, row) for row in cur.fetchall()]
    return {"dead": len(rows), "rows": rows}


@api.get("/v1/health/detail")
def health_detail() -> dict[str, Any]:
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
            by_source_status: dict[str, dict[str, int]] = {}
            for source, status, count in cur.fetchall():
                by_source_status.setdefault(source, {})[status] = count

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE status = 'in_progress'
                  AND lease_expires_at < now()
                """
            )
            stuck = cur.fetchone()[0]

            # procrastinate_jobs.worker_id is
            # REFERENCES procrastinate_workers(id) ON DELETE SET NULL, so after
            # pruning, any job left behind shows as doing with a null worker_id.
            # This should be zero in steady state — it is an ALERT condition,
            # not the primary recovery mechanism, because the sweeper's
            # get_stalled_jobs/retry_job should have caught them first.
            cur.execute(
                """
                SELECT count(*)::int
                FROM procrastinate_jobs
                WHERE status = 'doing'
                  AND worker_id IS NULL
                """
            )
            orphans = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*)::int
                FROM procrastinate_workers
                WHERE now() - last_heartbeat < interval '60 seconds'
                """
            )
            live_workers = cur.fetchone()[0]

            # Deployed worker count / identity visible over HTTP without shell.
            cur.execute(
                """
                SELECT id,
                       EXTRACT(EPOCH FROM (now() - last_heartbeat))::float
                         AS heartbeat_age_seconds
                FROM procrastinate_workers
                ORDER BY id
                """
            )
            workers = [
                {"id": wid, "heartbeat_age_seconds": age}
                for wid, age in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                  AND raw_written_at < now() - interval '15 minutes'
                """
            )
            unloaded = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE status = 'dead'
                """
            )
            dead = cur.fetchone()[0]

    return {
        "counts": by_source_status,
        "stuck": stuck,
        "orphans": orphans,
        "live_workers": live_workers,
        "workers": workers,
        "unloaded": unloaded,
        "dead": dead,
    }
