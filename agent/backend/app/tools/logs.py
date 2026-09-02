"""SigNoz logs / traces / metrics tools.

If SigNoz is not configured, every tool returns a clear unavailable payload
instead of raising — the agent must still answer SQL/BQ/GCP questions.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.clients.signoz import QUERY_PATH
from app.tools.base import (
    MAX_RANGE_DAYS,
    SIGNOZ_UNAVAILABLE,
    TimeRangeArgs,
    ToolContext,
    ToolDef,
    jsonable,
    tool_result,
    validate_time_range,
)


class SearchLogsArgs(TimeRangeArgs):
    query: str = Field(
        default="",
        description=(
            "SigNoz filter expression, e.g. "
            "service.name = 'lisn-worker-sentinel' AND body CONTAINS 'dead'"
        ),
    )
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    service: str | None = Field(
        default=None,
        description="Optional service.name filter, e.g. lisn-worker-sentinel",
    )


class TracesForRequestArgs(BaseModel):
    request_id: str = Field(min_length=1)
    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None
    limit: int = Field(default=100, ge=1, le=500)

    model_config = {"populate_by_name": True}

    @field_validator("request_id")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _check_span_when_both(self) -> TracesForRequestArgs:
        if self.from_ is not None and self.to is not None:
            validate_time_range(self.from_, self.to, max_days=MAX_RANGE_DAYS)
        return self


class GetMetricArgs(TimeRangeArgs):
    name: str = Field(
        min_length=1,
        description="Metric name, e.g. lisn.page.duration or signoz_calls_total",
    )


def _signoz_unavailable() -> dict[str, Any]:
    return dict(SIGNOZ_UNAVAILABLE)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def search_logs(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Search SigNoz logs for a filter expression over a time range.

    WHEN TO USE: "what did the logs say", after diagnose_incident / get_failed_jobs
    gives a request_id or worker_id and you need the log lines. Prefer
    diagnose_incident first for collection status — logs explain, they do not
    replace the warehouse/ledger chain.

    Returns data = list of log records (raw query_range payload rows when
    available). Empty list means no matching logs in range. If SigNoz is not
    configured, returns error="SigNoz is not configured in this environment"
    without raising.

    Time range required; maximum span 31 days. Default limit 100.
    """
    assert isinstance(args, SearchLogsArgs)
    if not ctx.signoz.configured:
        return _signoz_unavailable()

    parts: list[str] = []
    if args.query.strip():
        parts.append(f"({args.query.strip()})")
    if args.service:
        parts.append(f"service.name = '{args.service}'")
    if args.level:
        parts.append(f"severity_text = '{args.level}'")
    filter_expr = " AND ".join(parts) if parts else "true"

    payload = {
        "start": _ms(args.range_from),
        "end": _ms(args.range_to),
        "requestType": "raw",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "logs",
                        "stepInterval": 60,
                        "filter": {"expression": filter_expr},
                        "order": [
                            {"key": {"name": "timestamp"}, "direction": "desc"}
                        ],
                        "limit": args.limit,
                        "offset": 0,
                        "disabled": False,
                    },
                }
            ]
        },
    }
    query = f"POST {QUERY_PATH} {json.dumps(payload, sort_keys=True)}"
    try:
        body = ctx.signoz.query_range(payload)
        data = _extract_rows(body)
        return tool_result(data=jsonable(data), query=query, row_count=len(data))
    except Exception as exc:  # noqa: BLE001 — surface as empty+error, don't crash agent
        return {
            "data": [],
            "query": query,
            "row_count": 0,
            "error": str(exc),
        }


def get_traces_for_request(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Fetch SigNoz traces/spans tagged with a collector request_id.

    WHEN TO USE: you have a request_id from diagnose_incident or get_request_status
    and need the distributed trace (API collect_request → worker fetch_page).
    Not a substitute for ledger status.

    If from/to omitted, defaults to the last 7 days (still span-capped).
    If SigNoz is not configured, returns the unavailable payload without raising.
    """
    assert isinstance(args, TracesForRequestArgs)
    if not ctx.signoz.configured:
        return _signoz_unavailable()

    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    range_from = args.from_ or (now - timedelta(days=7))
    range_to = args.to or now
    range_from, range_to = validate_time_range(
        range_from, range_to, max_days=MAX_RANGE_DAYS
    )

    filter_expr = (
        f"request_id = '{args.request_id}' OR "
        f"lisn.request_id = '{args.request_id}' OR "
        f"attributes.string.request_id = '{args.request_id}'"
    )
    payload = {
        "start": _ms(range_from),
        "end": _ms(range_to),
        "requestType": "raw",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "traces",
                        "stepInterval": 60,
                        "filter": {"expression": filter_expr},
                        "limit": args.limit,
                        "disabled": False,
                    },
                }
            ]
        },
    }
    query = f"POST {QUERY_PATH} {json.dumps(payload, sort_keys=True)}"
    try:
        body = ctx.signoz.query_range(payload)
        data = _extract_rows(body)
        return tool_result(data=jsonable(data), query=query, row_count=len(data))
    except Exception as exc:  # noqa: BLE001
        return {
            "data": [],
            "query": query,
            "row_count": 0,
            "error": str(exc),
        }


def get_metric(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Query one SigNoz metric as a time series over a range.

    WHEN TO USE: rate / latency / gauge questions (e.g. page duration, live
    workers) when the numeric series matters more than individual log lines.
    Prefer collector SQL gauges via diagnose/state tools when the question is
    about ledger state, not telemetry.

    Returns data = time series points from query_range (or []). If SigNoz is
    not configured, returns the unavailable payload without raising.

    Time range required; maximum span 31 days.
    """
    assert isinstance(args, GetMetricArgs)
    if not ctx.signoz.configured:
        return _signoz_unavailable()

    payload = {
        "start": _ms(args.range_from),
        "end": _ms(args.range_to),
        "requestType": "time_series",
        "compositeQuery": {
            "queries": [
                {
                    "type": "builder_query",
                    "spec": {
                        "name": "A",
                        "signal": "metrics",
                        "stepInterval": 60,
                        "aggregations": [
                            {
                                "metricName": args.name,
                                "timeAggregation": "avg",
                                "spaceAggregation": "avg",
                            }
                        ],
                        "disabled": False,
                    },
                }
            ]
        },
    }
    query = f"POST {QUERY_PATH} {json.dumps(payload, sort_keys=True)}"
    try:
        body = ctx.signoz.query_range(payload)
        data = _extract_rows(body)
        return tool_result(data=jsonable(data), query=query, row_count=len(data))
    except Exception as exc:  # noqa: BLE001
        return {
            "data": [],
            "query": query,
            "row_count": 0,
            "error": str(exc),
        }


def _extract_rows(body: dict[str, Any]) -> list[Any]:
    """Best-effort flatten of SigNoz v5 query_range responses."""
    if not body:
        return []
    # Common shapes: {data: {result: [...]}} or {data: [...]} or {result: [...]}
    data = body.get("data", body)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("result", "results", "rows", "items"):
            val = data.get(key)
            if isinstance(val, list):
                return val
        # Keep a single envelope so the caller can inspect raw structure.
        return [data]
    return []


TOOLS: list[ToolDef] = [
    ToolDef(
        name="search_logs",
        description=search_logs.__doc__ or "",
        args_schema=SearchLogsArgs,
        handler=search_logs,
    ),
    ToolDef(
        name="get_traces_for_request",
        description=get_traces_for_request.__doc__ or "",
        args_schema=TracesForRequestArgs,
        handler=get_traces_for_request,
    ),
    ToolDef(
        name="get_metric",
        description=get_metric.__doc__ or "",
        args_schema=GetMetricArgs,
        handler=get_metric,
    ),
]
