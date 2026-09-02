"""Collector Cloud SQL state tools — windows, jobs, requests, workers.

Use diagnose_* tools first for collection/gap questions. These are for
inspecting ledger rows the chain already pointed at.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.redact import redact_secrets
from app.tools.base import (
    LimitMixin,
    TimeRangeArgs,
    ToolContext,
    ToolDef,
    jsonable,
    tool_result,
)


class DiscoveryWindowsArgs(TimeRangeArgs):
    source: str = Field(default="sentinel")


class FailedJobsArgs(TimeRangeArgs):
    source: str = Field(
        default="sentinel",
        description="Collector source filter, e.g. sentinel or sentinel_discovery.",
    )


class RequestStatusArgs(LimitMixin):
    request_id: str = Field(min_length=1)

    @field_validator("request_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        value = value.strip()
        UUID(value)  # raises if malformed
        return value


class WorkerHistoryArgs(TimeRangeArgs):
    pass


def get_discovery_windows(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """List discovery_window rows overlapping a time range.

    WHEN TO USE: inspect which windows ran (status, id_count, partial) after
    diagnose_time_range / explain_gap pointed at a coverage problem. Not the
    first tool for "was incident X collected".

    Returns data = list of windows with status, id_count, and partial
    (true when status='partial'). Empty list means no overlapping windows —
    that is a schedule gap signal, not a query failure.

    Time range required; maximum span 31 days. Default limit 100.
    """
    assert isinstance(args, DiscoveryWindowsArgs)
    sql = """
SELECT
  window_id, source, request_id, window_field,
  window_from, window_to, id_count, status, allow_gap, gap_reason,
  started_at, completed_at,
  (status = 'partial') AS partial
FROM discovery_window
WHERE source = %(source)s
  AND window_field = 'updated_on'
  AND window_from < %(to_ts)s
  AND window_to > %(from_ts)s
ORDER BY window_from
LIMIT %(limit)s
"""
    params = {
        "source": args.source,
        "from_ts": args.range_from,
        "to_ts": args.range_to,
        "limit": args.limit,
    }
    rows = ctx.sql.fetch_collector(sql, params)
    return tool_result(
        data=jsonable(rows),
        query=sql.strip(),
        row_count=len(rows),
    )


def get_failed_jobs(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """List dead and failed collector_job pages in a time range.

    WHEN TO USE: "what failed between A and B", or after diagnose_incident
    returns ENRICHMENT_DEAD_LETTERED / ENRICHMENT_FAILED and you need siblings.
    last_error is redacted before return.

    Returns data = list of {job_id, request_id, source, page_no, status,
    attempts, last_error, created_at, updated_at}. Empty list means no
    dead/failed pages in range — not that enrichment succeeded.

    Time range required; maximum span 31 days. Default limit 100.
    """
    assert isinstance(args, FailedJobsArgs)
    sql = """
SELECT
  job_id, request_id, source, page_no, status, attempts,
  last_error, created_at, updated_at
FROM collector_job
WHERE source = %(source)s
  AND status IN ('dead', 'failed')
  AND created_at >= %(from_ts)s
  AND created_at < %(to_ts)s
ORDER BY created_at DESC
LIMIT %(limit)s
"""
    params = {
        "source": args.source,
        "from_ts": args.range_from,
        "to_ts": args.range_to,
        "limit": args.limit,
    }
    rows = ctx.sql.fetch_collector(sql, params)
    for row in rows:
        if row.get("last_error") is not None:
            row["last_error"] = redact_secrets(str(row["last_error"]))
    return tool_result(
        data=jsonable(rows),
        query=sql.strip(),
        row_count=len(rows),
    )


def get_request_status(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Page counts by status, timings, and owners for one collector_request.

    WHEN TO USE: you have a request_id (from diagnose_incident, a window, or a
    job) and need "how far along is this request / who owns open pages".

    Returns data = {
      request: {...} | null,
      pages_by_status: {status: count},
      pages: [up to limit page rows with owner/timings]
    }
    If the request id is unknown, request is null and pages is [] — not an error.
    """
    assert isinstance(args, RequestStatusArgs)
    req_sql = """
SELECT request_id, source, query_spec, total_pages, status, created_at, closed_at
FROM collector_request
WHERE request_id = %(request_id)s::uuid
LIMIT 1
"""
    req_params = {"request_id": args.request_id}
    requests = ctx.sql.fetch_collector(req_sql, req_params)

    counts_sql = """
SELECT status, count(*)::int AS n
FROM collector_job
WHERE request_id = %(request_id)s::uuid
GROUP BY status
ORDER BY status
"""
    counts = ctx.sql.fetch_collector(counts_sql, req_params)
    pages_by_status = {str(r["status"]): int(r["n"]) for r in counts}

    pages_sql = """
SELECT
  job_id, page_no, status, attempts, owner,
  lease_expires_at, record_count, requested_count, returned_count,
  last_error, created_at, updated_at, raw_written_at, loaded_at
FROM collector_job
WHERE request_id = %(request_id)s::uuid
ORDER BY page_no
LIMIT %(limit)s
"""
    pages_params = {"request_id": args.request_id, "limit": args.limit}
    pages = ctx.sql.fetch_collector(pages_sql, pages_params)
    for row in pages:
        if row.get("last_error") is not None:
            row["last_error"] = redact_secrets(str(row["last_error"]))

    data = {
        "request": jsonable(requests[0]) if requests else None,
        "pages_by_status": pages_by_status,
        "pages": jsonable(pages),
    }
    query = (
        f"-- request\n{req_sql.strip()}\n\n"
        f"-- counts\n{counts_sql.strip()}\n\n"
        f"-- pages\n{pages_sql.strip()}"
    )
    return tool_result(
        data=data,
        query=query,
        row_count=len(pages),
    )


def get_worker_history(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Worker heartbeats near a time range — "was anything running then?".

    WHEN TO USE: explain_gap / no_workers follow-up, or "were enrichment
    workers alive during this outage". procrastinate_workers stores current
    workers (id + last_heartbeat) only — not a historical log. Empty data
    means no worker rows with heartbeats near the window *now*, not proof
    that none ran then; cross-check get_job_executions for Cloud Run history.

    Returns data = list of {worker_id, last_heartbeat, heartbeat_age}.
    Time range required; maximum span 31 days. Default limit 100.
    """
    assert isinstance(args, WorkerHistoryArgs)
    sql = """
SELECT
  id AS worker_id,
  last_heartbeat,
  now() - last_heartbeat AS heartbeat_age
FROM procrastinate_workers
WHERE last_heartbeat >= %(from_ts)s - interval '1 day'
  AND last_heartbeat < %(to_ts)s + interval '1 day'
ORDER BY last_heartbeat DESC
LIMIT %(limit)s
"""
    params = {
        "from_ts": args.range_from,
        "to_ts": args.range_to,
        "limit": args.limit,
    }
    rows = ctx.sql.fetch_collector(sql, params)
    return tool_result(
        data=jsonable(rows),
        query=sql.strip(),
        row_count=len(rows),
    )


TOOLS: list[ToolDef] = [
    ToolDef(
        name="get_discovery_windows",
        description=get_discovery_windows.__doc__ or "",
        args_schema=DiscoveryWindowsArgs,
        handler=get_discovery_windows,
    ),
    ToolDef(
        name="get_failed_jobs",
        description=get_failed_jobs.__doc__ or "",
        args_schema=FailedJobsArgs,
        handler=get_failed_jobs,
    ),
    ToolDef(
        name="get_request_status",
        description=get_request_status.__doc__ or "",
        args_schema=RequestStatusArgs,
        handler=get_request_status,
    ),
    ToolDef(
        name="get_worker_history",
        description=get_worker_history.__doc__ or "",
        args_schema=WorkerHistoryArgs,
        handler=get_worker_history,
    ),
]
