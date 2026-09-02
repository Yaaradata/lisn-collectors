"""BigQuery warehouse tools — incident presence and collection volume.

Use diagnose_incident / diagnose_time_range first for "was it collected" and
"what is missing". These tools are for follow-up numbers and ratios.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from google.cloud import bigquery
from pydantic import BaseModel

from app.tools.base import (
    IncidentIdArgs,
    LimitMixin,
    TimeRangeArgs,
    ToolContext,
    ToolDef,
    jsonable,
    tool_result,
)


class CheckIncidentCollectedArgs(IncidentIdArgs, LimitMixin):
    pass


class CollectionStatsArgs(TimeRangeArgs):
    pass


class CompareSourceArgs(TimeRangeArgs):
    pass


def _partition_pad(range_from: datetime, range_to: datetime) -> tuple[datetime, datetime]:
    return range_from - timedelta(days=30), range_to + timedelta(days=30)


def check_incident_collected(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Check whether one incident id appears in sentinel_core.incidents_current.

    WHEN TO USE: follow-up after diagnose_incident, or when you only need a
    yes/no + thread row count and already know you do not need the full gap
    chain. For "was X collected and why not", call diagnose_incident instead.

    Returns data = list with at most one row:
      {id, thread_rows, collected_at, request_id}
    Empty list means not present in the partition lookback — not an error.
    """
    assert isinstance(args, CheckIncidentCollectedArgs)
    settings = ctx.settings
    now = datetime.now(timezone.utc)
    p_from = now - timedelta(days=400)
    p_to = now + timedelta(days=1)
    fqn = f"`{settings.gcp_project}.{settings.bq_core_dataset}.incidents_current`"
    sql = f"""
SELECT
  id,
  COUNT(*) AS thread_rows,
  MAX(_ingested_at) AS collected_at,
  ARRAY_AGG(_request_id ORDER BY _ingested_at DESC LIMIT 1)[OFFSET(0)] AS request_id
FROM {fqn}
WHERE id = @incident_id
  AND _ingested_at >= @p_from
  AND _ingested_at < @p_to
GROUP BY id
LIMIT @limit
"""
    params = [
        bigquery.ScalarQueryParameter("incident_id", "STRING", args.incident_id),
        bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
        bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
        bigquery.ScalarQueryParameter("limit", "INT64", args.limit),
    ]
    rows = ctx.bq.query(sql, params=params)
    return tool_result(data=jsonable(rows), query=sql.strip(), row_count=len(rows))


def get_collection_stats(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Collection volume stats for a time range over sentinel_raw landing rows.

    WHEN TO USE: "how much did we collect between A and B", "are we re-collecting
    the same incidents". Filters on updatedOn with an _ingested_at partition
    bound. Returns one summary object:
      rows, distinct_incidents, distinct_requests,
      distinct_id_thread_pairs, copies_ratio

    IMPORTANT — copies_ratio: rows / distinct (id, threads_id) pairs. A ratio
    above 1 means the same data was collected more than once. That is expected
    — raw is append-only — and is NOT a defect. Do not report it as a problem;
    only flag it if the operator explicitly asks about duplicate collections.

    Time range required; maximum span 31 days.
    """
    assert isinstance(args, CollectionStatsArgs)
    settings = ctx.settings
    p_from, p_to = _partition_pad(args.range_from, args.range_to)
    fqn = (
        f"`{settings.gcp_project}.{settings.bq_raw_dataset}."
        f"{settings.bq_landing_table}`"
    )
    # Never count(*) alone for "how many incidents" — use DISTINCT.
    sql = f"""
SELECT
  COUNT(*) AS row_count,
  COUNT(DISTINCT id) AS distinct_incidents,
  COUNT(DISTINCT _request_id) AS distinct_requests,
  COUNT(DISTINCT CONCAT(id, '|', IFNULL(threads_id, ''))) AS distinct_id_thread_pairs
FROM {fqn}
WHERE updatedOn >= @from_ts
  AND updatedOn < @to_ts
  AND _ingested_at >= @p_from
  AND _ingested_at < @p_to
"""
    params = [
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", args.range_from),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", args.range_to),
        bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
        bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
    ]
    rows = ctx.bq.query(sql, params=params)
    if not rows:
        summary = {
            "rows": 0,
            "distinct_incidents": 0,
            "distinct_requests": 0,
            "distinct_id_thread_pairs": 0,
            "copies_ratio": None,
        }
    else:
        row = rows[0]
        pairs = int(row["distinct_id_thread_pairs"] or 0)
        raw_rows = int(row["row_count"] or 0)
        summary = {
            "rows": raw_rows,
            "distinct_incidents": int(row["distinct_incidents"] or 0),
            "distinct_requests": int(row["distinct_requests"] or 0),
            "distinct_id_thread_pairs": pairs,
            "copies_ratio": (raw_rows / pairs) if pairs else None,
            "note": (
                "copies_ratio > 1 is expected for append-only raw — "
                "not a defect"
            ),
        }
    return tool_result(data=summary, query=sql.strip(), row_count=1)


def compare_source_to_warehouse(ctx: ToolContext, args: BaseModel) -> dict[str, Any]:
    """Compare source incident count to warehouse distinct ids for a time range.

    WHEN TO USE: a quick numeric gap (source_count vs warehouse_count) without
    the full diagnose_time_range payload. Prefer diagnose_time_range when you
    also need windows, gaps, and failed pages.

    Returns data = {source_count, warehouse_count, missing, from, to}.
    Time range required; maximum span 31 days.
    """
    assert isinstance(args, CompareSourceArgs)
    settings = ctx.settings
    source_sql = """
SELECT count(*)::int AS n
FROM sentinel_incident
WHERE updated_on >= %(from_ts)s
  AND updated_on < %(to_ts)s
"""
    source_params = {"from_ts": args.range_from, "to_ts": args.range_to}
    source_rows = ctx.sql.fetch_sentinel_mock(source_sql, source_params)
    source_count = int(source_rows[0]["n"]) if source_rows else 0

    p_from, p_to = _partition_pad(args.range_from, args.range_to)
    fqn = f"`{settings.gcp_project}.{settings.bq_core_dataset}.incidents_current`"
    bq_sql = f"""
SELECT COUNT(DISTINCT id) AS n
FROM {fqn}
WHERE updatedOn >= @from_ts
  AND updatedOn < @to_ts
  AND _ingested_at >= @p_from
  AND _ingested_at < @p_to
"""
    bq_params = [
        bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", args.range_from),
        bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", args.range_to),
        bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
        bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
    ]
    wh_rows = ctx.bq.query(bq_sql, params=bq_params)
    warehouse_count = int(wh_rows[0]["n"]) if wh_rows else 0
    data = {
        "from": args.range_from.isoformat(),
        "to": args.range_to.isoformat(),
        "source_count": source_count,
        "warehouse_count": warehouse_count,
        "missing": source_count - warehouse_count,
    }
    query = (
        f"-- source\n{source_sql.strip()}\n\n-- warehouse\n{bq_sql.strip()}"
    )
    return tool_result(data=data, query=query, row_count=1)


TOOLS: list[ToolDef] = [
    ToolDef(
        name="check_incident_collected",
        description=check_incident_collected.__doc__ or "",
        args_schema=CheckIncidentCollectedArgs,
        handler=check_incident_collected,
    ),
    ToolDef(
        name="get_collection_stats",
        description=get_collection_stats.__doc__ or "",
        args_schema=CollectionStatsArgs,
        handler=get_collection_stats,
    ),
    ToolDef(
        name="compare_source_to_warehouse",
        description=compare_source_to_warehouse.__doc__ or "",
        args_schema=CompareSourceArgs,
        handler=compare_source_to_warehouse,
    ),
]
