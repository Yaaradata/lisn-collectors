"""Shared tool contract helpers.

Every tool returns {"data", "query", "row_count"}. Empty results are empty
lists (or empty dicts), never exceptions. Strings that might carry credentials
are redacted before return.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.clients.bq import BigQueryClient
from app.clients.gcp import GcpRunClient
from app.clients.signoz import SignozClient
from app.clients.sql import SqlClient
from app.config import Settings
from app.diagnostics import Diagnostics
from app.redact import redact_tree

DEFAULT_LIMIT = 100
MAX_LIMIT = 500
# Ops questions spanning months become full-partition BQ scans. Cap the span.
MAX_RANGE_DAYS = 31

SIGNOZ_UNAVAILABLE = {
    "data": [],
    "query": "SigNoz not configured — would POST /api/v5/query_range",
    "row_count": 0,
    "error": "SigNoz is not configured in this environment",
}


@dataclass
class ToolContext:
    """Clients constructed once at startup — tools never open their own pools."""

    sql: SqlClient
    bq: BigQueryClient
    signoz: SignozClient
    gcp: GcpRunClient
    settings: Settings
    diagnostics: Diagnostics


class ToolDef(BaseModel):
    """One bindable tool for Pass 5. Handler is excluded from serialization."""

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    args_schema: type[BaseModel]
    handler: Callable[[ToolContext, BaseModel], dict[str, Any]]

    def invoke(self, ctx: ToolContext, raw_args: dict[str, Any]) -> dict[str, Any]:
        args = self.args_schema.model_validate(raw_args)
        return self.handler(ctx, args)


def tool_result(
    *,
    data: Any,
    query: str,
    row_count: int | None = None,
) -> dict[str, Any]:
    if row_count is None:
        if isinstance(data, list):
            row_count = len(data)
        elif data is None:
            row_count = 0
        else:
            row_count = 1
    return {
        "data": redact_tree(data),
        "query": query,
        "row_count": row_count,
    }


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def validate_time_range(
    range_from: datetime,
    range_to: datetime,
    *,
    max_days: int = MAX_RANGE_DAYS,
) -> tuple[datetime, datetime]:
    start = utc(range_from)
    end = utc(range_to)
    if start >= end:
        raise ValueError("from must be earlier than to")
    if end - start > timedelta(days=max_days):
        raise ValueError(
            f"time range exceeds maximum span of {max_days} days "
            f"(got {(end - start).days} days)"
        )
    return start, end


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


class LimitMixin(BaseModel):
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description="Max rows to return (default 100, max 500).",
    )


class TimeRangeArgs(LimitMixin):
    """Common from/to window with span cap. Use wherever a time range is meaningful."""

    from_: datetime = Field(alias="from")
    to: datetime

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_span(self) -> TimeRangeArgs:
        validate_time_range(self.from_, self.to)
        return self

    @property
    def range_from(self) -> datetime:
        return utc(self.from_)

    @property
    def range_to(self) -> datetime:
        return utc(self.to)


class IncidentIdArgs(BaseModel):
    incident_id: str = Field(min_length=1)

    @field_validator("incident_id")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("incident_id is required")
        return value
