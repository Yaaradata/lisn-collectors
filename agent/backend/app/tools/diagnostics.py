"""Diagnostic-chain tools — FIRST choice for collection/gap questions.

Prefer these over assembling warehouse/state/logs primitives. The chain is
deterministic; rediscovering it from primitives skips steps.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.tools.base import (
    IncidentIdArgs,
    TimeRangeArgs,
    ToolContext,
    ToolDef,
    jsonable,
    tool_result,
)


class DiagnoseIncidentArgs(IncidentIdArgs):
    pass


class DiagnoseRangeArgs(TimeRangeArgs):
    # Range diagnosis aggregates counts — limit unused but kept for contract.
    limit: int = Field(default=100, ge=1, le=500)


class ExplainGapArgs(TimeRangeArgs):
    pass


def diagnose_incident(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """FIRST CHOICE for "was incident X collected?" and "why not?".

    Runs the deterministic Pass-2 chain (warehouse → discovered_ids → source →
    discovery_window → enrichment job) and returns a structured IncidentDiagnosis
    with verdict + inspectable steps. Do NOT rebuild this sequence from
    check_incident_collected / get_discovery_windows / get_failed_jobs — the
    model will sometimes skip a step and answer "not collected" when the truth
    is truncation.

    Returns data = full diagnosis object; query = concatenated step SQL/API
    text so a human can re-run every check.
    """
    assert isinstance(args, DiagnoseIncidentArgs)
    result = ctx.diagnostics.diagnose_incident(args.incident_id)
    query_parts = [
        f"-- step {s.step}: {s.name} [{s.system}]\n{s.query}" for s in result.steps
    ]
    return tool_result(
        data=jsonable(result.model_dump(mode="json")),
        query="\n\n".join(query_parts) or "(no steps)",
        row_count=len(result.steps),
    )


def diagnose_time_range(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """FIRST CHOICE for "what happened between A and B?" / how many are missing.

    Returns source_count, warehouse_count, discovered_count, missing, overlapping
    discovery windows, gaps, partial windows, and failed pages in one call.
    Prefer this over calling compare_source_to_warehouse + get_discovery_windows
    + get_failed_jobs separately — those are for follow-up detail after the
    range diagnosis points at a problem.

    Time range required; maximum span 31 days.
    """
    assert isinstance(args, DiagnoseRangeArgs)
    result = ctx.diagnostics.diagnose_time_range(args.range_from, args.range_to)
    query_parts = [
        f"-- step {s.step}: {s.name} [{s.system}]\n{s.query}" for s in result.steps
    ]
    return tool_result(
        data=jsonable(result.model_dump(mode="json")),
        query="\n\n".join(query_parts),
        row_count=1,
    )


def explain_gap(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """FIRST CHOICE when you already know a gap range and need the cause.

    Classifies never_scheduled / truncated / failed_discovery / no_workers
    (or mixed), and includes worker heartbeat rows plus Cloud Run executions.
    Use after diagnose_time_range surfaces missing > 0 or an empty windows list.

    Time range required; maximum span 31 days.
    """
    assert isinstance(args, ExplainGapArgs)
    result = ctx.diagnostics.explain_gap(args.range_from, args.range_to)
    query_parts = [
        f"-- step {s.step}: {s.name} [{s.system}]\n{s.query}" for s in result.steps
    ]
    return tool_result(
        data=jsonable(result.model_dump(mode="json")),
        query="\n\n".join(query_parts),
        row_count=1,
    )


TOOLS: list[ToolDef] = [
    ToolDef(
        name="diagnose_incident",
        description=diagnose_incident.__doc__ or "",
        args_schema=DiagnoseIncidentArgs,
        handler=diagnose_incident,
    ),
    ToolDef(
        name="diagnose_time_range",
        description=diagnose_time_range.__doc__ or "",
        args_schema=DiagnoseRangeArgs,
        handler=diagnose_time_range,
    ),
    ToolDef(
        name="explain_gap",
        description=explain_gap.__doc__ or "",
        args_schema=ExplainGapArgs,
        handler=explain_gap,
    ),
]
