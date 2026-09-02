"""Bridge Pass-3 ToolDef registry → LangChain StructuredTool for ToolNode."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from app.redact import redact_tree
from app.tools import ALL_TOOLS
from app.tools.base import ToolContext, ToolDef


def build_langchain_tools(ctx: ToolContext) -> list[BaseTool]:
    """Bind ALL_TOOLS in one pass — each closes over ToolContext."""
    return [_to_structured(ctx, tool) for tool in ALL_TOOLS]


def _to_structured(ctx: ToolContext, tool: ToolDef) -> StructuredTool:
    tool_def = tool

    def _run(**kwargs: Any) -> str:
        try:
            result = tool_def.invoke(ctx, kwargs)
        except Exception as exc:  # noqa: BLE001 — surface to the model, don't crash the turn
            # An operator must hear that a tool failed. Raising here aborts the
            # graph and the agent invents nothing — or worse, invents a guess.
            result = {
                "data": [],
                "query": f"{tool_def.name}({kwargs!r})",
                "row_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        # Models consume a string; keep the full contract (data/query/row_count).
        return json.dumps(redact_tree(result), default=str)

    _run.__name__ = tool.name
    _run.__doc__ = tool.description
    return StructuredTool.from_function(
        func=_run,
        name=tool.name,
        description=(tool.description or tool.name).strip(),
        args_schema=tool.args_schema,
    )
