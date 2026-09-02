"""Tool registry — Pass 5 binds ALL_TOOLS in one line."""

from __future__ import annotations

from app.tools.base import ToolContext, ToolDef
from app.tools.diagnostics import TOOLS as DIAGNOSTIC_TOOLS
from app.tools.infra import TOOLS as INFRA_TOOLS
from app.tools.logs import TOOLS as LOG_TOOLS
from app.tools.state import TOOLS as STATE_TOOLS
from app.tools.warehouse import TOOLS as WAREHOUSE_TOOLS

ALL_TOOLS: list[ToolDef] = [
    *DIAGNOSTIC_TOOLS,
    *WAREHOUSE_TOOLS,
    *STATE_TOOLS,
    *LOG_TOOLS,
    *INFRA_TOOLS,
]

TOOLS_BY_NAME: dict[str, ToolDef] = {t.name: t for t in ALL_TOOLS}

__all__ = [
    "ALL_TOOLS",
    "TOOLS_BY_NAME",
    "ToolContext",
    "ToolDef",
]
