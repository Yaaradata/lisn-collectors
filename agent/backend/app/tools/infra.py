"""Cloud Run Jobs infrastructure tools — execution history.

Read-only listing of past executions. Never mutates job definitions and never
starts or cancels executions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.tools.base import (
    LimitMixin,
    ToolContext,
    ToolDef,
    jsonable,
    tool_result,
)


class JobExecutionsArgs(LimitMixin):
    job_name: str = Field(
        description=(
            "Cloud Run Job name: col-sentinel, col-sentinel-discovery, "
            "or col-maintenance"
        )
    )

    @field_validator("job_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


def get_job_executions(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """List Cloud Run Job executions with status and termination reason.

    WHEN TO USE: "was the worker killed", "did the 24-hour task timeout fire",
    "was an execution cancelled / exited non-zero". This is the ONLY source
    that knows a task was terminated at the 24-hour ceiling, or was cancelled,
    or exited non-zero. Those events do NOT appear in the collector's own
    tables (collector_job / procrastinate_workers).

    Returns data = list of execution summaries (name, counts, conditions /
    termination messages). Empty list means no executions returned for that
    job — not that the job never ran historically beyond the API page.

    Default limit 100 (capped). Allowed job names come from settings.
    """
    assert isinstance(args, JobExecutionsArgs)
    query = (
        f"ExecutionsClient.list_executions("
        f"projects/{ctx.settings.gcp_project}/locations/{ctx.settings.gcp_region}"
        f"/jobs/{args.job_name}, page_size={args.limit})"
    )
    try:
        rows = ctx.gcp.list_executions(args.job_name, page_size=args.limit)
    except ValueError as exc:
        return tool_result(data=[], query=query + f"  # rejected: {exc}", row_count=0)
    return tool_result(data=jsonable(rows), query=query, row_count=len(rows))


TOOLS: list[ToolDef] = [
    ToolDef(
        name="get_job_executions",
        description=get_job_executions.__doc__ or "",
        args_schema=JobExecutionsArgs,
        handler=get_job_executions,
    ),
]
