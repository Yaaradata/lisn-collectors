"""Tool-layer contract tests against real clients."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from google.api_core.exceptions import Forbidden
from google.cloud import bigquery
from pydantic import ValidationError

from app.clients.bq import BigQueryClient
from app.tools import ALL_TOOLS, TOOLS_BY_NAME
from app.tools.base import MAX_RANGE_DAYS, TimeRangeArgs


# ---------------------------------------------------------------------------
# Per-tool contract
# ---------------------------------------------------------------------------

# Tools that require a time range in their args schema.
_TIME_RANGE_TOOLS = {
    name
    for name, tool in TOOLS_BY_NAME.items()
    if "from" in tool.args_schema.model_fields
    or "from_" in tool.args_schema.model_fields
}

# Tools that expose a limit field.
_LIMIT_TOOLS = {
    name
    for name, tool in TOOLS_BY_NAME.items()
    if "limit" in tool.args_schema.model_fields
}


@pytest.mark.parametrize("tool_name", [t.name for t in ALL_TOOLS])
def test_tool_returns_query_field(tool_ctx, tool_name, failed_job_with_dsn):
    tool = TOOLS_BY_NAME[tool_name]
    args = _sample_args(tool_name)
    result = tool.invoke(tool_ctx, args)
    assert "query" in result
    assert result["query"] is not None
    assert "data" in result
    assert "row_count" in result


@pytest.mark.parametrize("tool_name", sorted(_LIMIT_TOOLS))
def test_tool_enforces_row_limit(tool_name):
    tool = TOOLS_BY_NAME[tool_name]
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate(_sample_args(tool_name, limit=10_000))
    # Accepted at the default / max boundary.
    ok = tool.args_schema.model_validate(_sample_args(tool_name, limit=100))
    assert ok.limit == 100


@pytest.mark.parametrize("tool_name", sorted(_TIME_RANGE_TOOLS))
def test_tool_enforces_time_range_maximum(tool_name):
    tool = TOOLS_BY_NAME[tool_name]
    too_wide = {
        **_sample_args(tool_name),
        "from": "2026-01-01T00:00:00Z",
        "to": "2026-03-15T00:00:00Z",  # > 31 days
    }
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate(too_wide)

    # Span exactly at the max is ok.
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=MAX_RANGE_DAYS)
    ok_args = {**_sample_args(tool_name), "from": start.isoformat(), "to": end.isoformat()}
    if tool_name == "get_traces_for_request":
        # optional from/to — still validate when both provided via TimeRange path
        pass
    tool.args_schema.model_validate(ok_args)


@pytest.mark.parametrize(
    "tool_name,args",
    [
        (
            "get_discovery_windows",
            {"from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"},
        ),
        (
            "get_failed_jobs",
            {
                "from": "2020-01-01T00:00:00Z",
                "to": "2020-01-02T00:00:00Z",
                "source": "sentinel",
            },
        ),
        (
            "get_worker_history",
            {"from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"},
        ),
        (
            "check_incident_collected",
            {"incident_id": "IN_NO_MATCH_EMPTY_LIST_000"},
        ),
        (
            "get_job_executions",
            {"job_name": "col-sentinel", "limit": 1},
        ),
    ],
)
def test_tool_empty_list_not_raise(tool_ctx, tool_name, args):
    result = TOOLS_BY_NAME[tool_name].invoke(tool_ctx, args)
    assert "data" in result
    assert "query" in result
    if tool_name == "get_job_executions":
        assert isinstance(result["data"], list)
        return
    if isinstance(result["data"], list):
        assert result["data"] == []
        assert result["row_count"] == 0


def test_signoz_tools_unavailable_not_raise(tool_ctx):
    for name in ("search_logs", "get_traces_for_request", "get_metric"):
        result = TOOLS_BY_NAME[name].invoke(tool_ctx, _sample_args(name))
        assert result["data"] == []
        assert "query" in result and result["query"]
        if not tool_ctx.signoz.configured:
            assert "SigNoz is not configured" in (result.get("error") or "")


# ---------------------------------------------------------------------------
# Structural / safety
# ---------------------------------------------------------------------------

_WRITE_SQL = re.compile(
    r"\b("
    r"INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM|TRUNCATE\s+\w+|"
    r"DROP\s+(TABLE|INDEX|SCHEMA|DATABASE)|CREATE\s+(TABLE|INDEX|SCHEMA)|"
    r"ALTER\s+TABLE"
    r")\b",
    re.IGNORECASE | re.DOTALL,
)


def test_no_write_operations():
    """Structural guard: tools/clients must not embed write SQL statements.

    The deny-list regex in sql.py names the verbs it blocks; that is allowed.
    This test looks for executable SQL write shapes (INSERT INTO, DELETE FROM,
    …), not the English words in comments or the forbid-list itself.
    """
    roots = [
        Path(__file__).resolve().parents[1] / "app" / "tools",
        Path(__file__).resolve().parents[1] / "app" / "clients",
    ]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            # Strip docstrings and #-comments so narrative text cannot trip us.
            stripped = re.sub(r'""".*?"""', '""', text, flags=re.S)
            stripped = re.sub(r"'''.*?'''", "''", stripped, flags=re.S)
            stripped = re.sub(r"#.*", "", stripped)
            # Drop the readonly forbid-list assignment body.
            stripped = re.sub(
                r"_FORBIDDEN_RE\s*=\s*re\.compile\(.*?\)",
                "_FORBIDDEN_RE = None",
                stripped,
                flags=re.S,
            )
            if _WRITE_SQL.search(stripped):
                offenders.append(str(path))
    assert offenders == [], f"write SQL found in: {offenders}"


def test_bigquery_bytes_capped(settings, bq_client):
    # Every query path must set maximum_bytes_billed.
    cfg = bq_client._job_config(None)
    assert cfg.maximum_bytes_billed == settings.bq_max_bytes_billed
    assert cfg.maximum_bytes_billed > 0

    with pytest.raises(ValueError, match="maximum_bytes_billed"):
        bq_client._job_config(0)

    with pytest.raises(ValueError, match="maximum_bytes_billed"):
        bad = bigquery.QueryJobConfig(maximum_bytes_billed=None)
        bq_client.query("SELECT 1", job_config=bad)

    # A query that would scan more than the tiny cap must fail, not run.
    tiny = BigQueryClient(
        project=settings.gcp_project,
        location=settings.gcp_region,
        max_bytes_billed=1,  # 1 byte — any real table scan exceeds this
        raw_dataset=settings.bq_raw_dataset,
        core_dataset=settings.bq_core_dataset,
        landing_table=settings.bq_landing_table,
    )
    try:
        with pytest.raises(Exception) as exc_info:
            tiny.query(
                f"""
                SELECT COUNT(DISTINCT id) AS n
                FROM `{settings.gcp_project}.{settings.bq_raw_dataset}.{settings.bq_landing_table}`
                WHERE _ingested_at >= TIMESTAMP('2026-01-01')
                """
            )
        msg = str(exc_info.value).lower()
        assert (
            "bytes" in msg
            or "billing" in msg
            or "maximum_bytes_billed" in msg
            or "exceeded" in msg
        ), f"expected bytes-cap failure, got: {exc_info.value}"
    finally:
        tiny.close()


def test_credentials_redacted(tool_ctx, failed_job_with_dsn):
    result = TOOLS_BY_NAME["get_failed_jobs"].invoke(
        tool_ctx,
        {
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-04T00:00:00Z",
            "source": "sentinel",
            "limit": 100,
        },
    )
    assert result["query"]
    rows = result["data"]
    assert isinstance(rows, list)
    matching = [r for r in rows if str(r.get("job_id")) == failed_job_with_dsn]
    assert matching, "fixture dead job not returned"
    err = matching[0].get("last_error") or ""
    assert "s3cretPASS" not in err
    assert "***" in err
    assert "postgresql://" in err


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_args(tool_name: str, **overrides):
    base = {
        "diagnose_incident": {"incident_id": "IN270827PRECISION01"},
        "diagnose_time_range": {
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-23T00:00:00Z",
        },
        "explain_gap": {
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-23T00:00:00Z",
        },
        "check_incident_collected": {"incident_id": "IN270827PRECISION01"},
        "get_collection_stats": {
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-23T00:00:00Z",
        },
        "compare_source_to_warehouse": {
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-23T00:00:00Z",
        },
        "get_discovery_windows": {
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-23T00:00:00Z",
        },
        "get_failed_jobs": {
            "from": "2026-09-01T00:00:00Z",
            "to": "2026-09-04T00:00:00Z",
            "source": "sentinel",
        },
        "get_request_status": {
            "request_id": "f0e11015-2746-4ca0-b69f-ff6964b2c47b"
        },
        "get_worker_history": {
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-23T00:00:00Z",
        },
        "search_logs": {
            "query": "body CONTAINS 'page'",
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-21T00:00:00Z",
        },
        "get_traces_for_request": {
            "request_id": "f0e11015-2746-4ca0-b69f-ff6964b2c47b",
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-27T00:00:00Z",
        },
        "get_metric": {
            "name": "lisn.page.duration",
            "from": "2026-08-20T00:00:00Z",
            "to": "2026-08-21T00:00:00Z",
        },
        "get_job_executions": {"job_name": "col-sentinel", "limit": 5},
    }[tool_name]
    base.update(overrides)
    return base
