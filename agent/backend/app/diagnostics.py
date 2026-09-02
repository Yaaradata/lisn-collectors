"""Deterministic diagnostic chain for collector ops questions.

No model, no agent, no LangGraph. These functions encode the seven-check
sequence a human runs by hand so the answer is inspectable and repeatable.
The chat layer (Pass 5) narrates the structured result; it must not re-derive
the chain.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from google.cloud import bigquery
from pydantic import BaseModel, Field

from app.clients.bq import BigQueryClient
from app.clients.gcp import GcpRunClient
from app.clients.sql import SqlClient
from app.config import Settings

# ---------------------------------------------------------------------------
# Bounds — every query is capped. Vague questions must not become full scans.
# ---------------------------------------------------------------------------

SQL_ROW_LIMIT = 200
# Partition filter floor when the caller has no tighter bound. The pilot's
# landing tables are partitioned on DATE(_ingested_at); without a filter BQ
# scans every partition. 400 days covers the pilot horizon with headroom.
BQ_PARTITION_LOOKBACK_DAYS = 400

IncidentVerdict = Literal[
    "COLLECTED",
    "NOT_AT_SOURCE",
    "GAP_NOT_SCHEDULED",
    "GAP_TRUNCATED",
    "DISCOVERY_FAILED",
    "DISCOVERY_RUNNING",
    "UNEXPLAINED",
    "ENRICHMENT_DEAD_LETTERED",
    "ENRICHMENT_FAILED",
    "AWAITING_ENRICHMENT",
    "IN_PROGRESS",
    "DISCOVERED_NOT_QUEUED",
    "ENRICHMENT_DONE_MISSING_WAREHOUSE",
]

GapCause = Literal[
    "never_scheduled",
    "truncated",
    "failed_discovery",
    "no_workers",
    "mixed",
    "unknown",
]


class DiagnosisStep(BaseModel):
    """One explicit check. Empty result ≠ 'did not happen' — record it as empty."""

    step: int
    name: str
    system: Literal["bigquery", "cloud_sql_collector", "cloud_sql_source", "cloud_run"]
    query: str
    params: dict[str, Any] = Field(default_factory=dict)
    row_count: int
    result: Any = None
    note: str | None = None


class IncidentDiagnosis(BaseModel):
    incident_id: str
    verdict: IncidentVerdict
    summary: str
    steps: list[DiagnosisStep]
    collected_at: datetime | None = None
    request_id: str | None = None
    thread_rows: int | None = None
    source_updated_on: datetime | None = None
    last_error: str | None = None
    # Populated for coverage verdicts so callers can assert the incident's
    # updated_on falls inside the gap / truncated window that explains it.
    gap_from: datetime | None = None
    gap_to: datetime | None = None
    covering_window_id: str | None = None
    covering_window_id_count: int | None = None


class RangeDiagnosis(BaseModel):
    range_from: datetime
    range_to: datetime
    source_count: int
    warehouse_count: int
    discovered_count: int
    missing: int
    windows: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    partial_windows: list[dict[str, Any]]
    failed_pages: list[dict[str, Any]]
    steps: list[DiagnosisStep]


class GapExplanation(BaseModel):
    gap_from: datetime
    gap_to: datetime
    cause: GapCause
    summary: str
    windows: list[dict[str, Any]]
    gaps: list[dict[str, Any]]
    failed_windows: list[dict[str, Any]]
    worker_heartbeats: list[dict[str, Any]]
    cloud_run_executions: list[dict[str, Any]]
    steps: list[DiagnosisStep]


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _partition_bounds(
    *,
    range_from: datetime | None = None,
    range_to: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Inclusive-ish _ingested_at window used to prune BQ partitions."""
    now = datetime.now(timezone.utc)
    if range_from is None and range_to is None:
        return now - timedelta(days=BQ_PARTITION_LOOKBACK_DAYS), now + timedelta(days=1)
    assert range_from is not None and range_to is not None
    # Ingestion can lag updated_on; pad so partition pruning does not hide rows.
    return (
        _utc(range_from) - timedelta(days=30),
        _utc(range_to) + timedelta(days=30),
    )


class Diagnostics:
    """Pure read-only diagnostic chain over SQL + BigQuery (+ Cloud Run for gaps)."""

    def __init__(
        self,
        *,
        sql: SqlClient,
        bq: BigQueryClient,
        settings: Settings,
        gcp: GcpRunClient | None = None,
    ) -> None:
        self.sql = sql
        self.bq = bq
        self.settings = settings
        self.gcp = gcp

    # ------------------------------------------------------------------
    # FUNCTION 1 — single incident
    # ------------------------------------------------------------------

    def diagnose_incident(self, incident_id: str) -> IncidentDiagnosis:
        steps: list[DiagnosisStep] = []
        incident_id = incident_id.strip()
        if not incident_id:
            raise ValueError("incident_id is required")

        # 1. Warehouse current view
        warehouse = self._step_warehouse_current(incident_id, steps)
        if warehouse is not None:
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="COLLECTED",
                summary=(
                    f"Incident {incident_id} is in "
                    f"{self.settings.bq_core_dataset}.incidents_current "
                    f"({warehouse['thread_rows']} thread row(s))."
                ),
                steps=steps,
                collected_at=warehouse["collected_at"],
                request_id=warehouse["request_id"],
                thread_rows=warehouse["thread_rows"],
            )

        # 2. Discovery landing
        discovered = self._step_discovered(incident_id, steps)
        if discovered:
            return self._diagnose_enrichment_gap(incident_id, steps)

        # 3. Source existence
        source_row = self._step_source(incident_id, steps)
        if source_row is None:
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="NOT_AT_SOURCE",
                summary=(
                    f"Incident {incident_id} is not in sentinel_mock.sentinel_incident. "
                    "The id does not exist at the source."
                ),
                steps=steps,
            )

        updated_on = source_row["updated_on"]
        # 4. Discovery windows covering updated_on
        return self._diagnose_discovery_coverage(
            incident_id, updated_on=updated_on, steps=steps
        )

    def _step_warehouse_current(
        self, incident_id: str, steps: list[DiagnosisStep]
    ) -> dict[str, Any] | None:
        p_from, p_to = _partition_bounds()
        fqn = (
            f"`{self.settings.gcp_project}."
            f"{self.settings.bq_core_dataset}.incidents_current`"
        )
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
LIMIT 1
"""
        params = [
            bigquery.ScalarQueryParameter("incident_id", "STRING", incident_id),
            bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
            bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
        ]
        rows = self.bq.query(sql, params=params)
        steps.append(
            DiagnosisStep(
                step=1,
                name="warehouse_incidents_current",
                system="bigquery",
                query=sql.strip(),
                params={
                    "incident_id": incident_id,
                    "p_from": p_from.isoformat(),
                    "p_to": p_to.isoformat(),
                },
                row_count=len(rows),
                result=_jsonable(rows),
                note=(
                    "Empty result means this id is not in incidents_current "
                    "within the partition window — not that the query failed."
                ),
            )
        )
        if not rows:
            return None
        row = rows[0]
        collected_at = row["collected_at"]
        if isinstance(collected_at, str):
            collected_at = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        return {
            "thread_rows": int(row["thread_rows"]),
            "collected_at": collected_at,
            "request_id": str(row["request_id"]) if row.get("request_id") else None,
        }

    def _step_discovered(
        self, incident_id: str, steps: list[DiagnosisStep]
    ) -> bool:
        p_from, p_to = _partition_bounds()
        fqn = (
            f"`{self.settings.gcp_project}."
            f"{self.settings.bq_raw_dataset}.discovered_ids`"
        )
        # Always COUNT DISTINCT / inspect by incident_id — append-only table.
        sql = f"""
SELECT
  incident_id,
  COUNT(*) AS discovery_rows,
  MAX(_ingested_at) AS last_ingested_at,
  ARRAY_AGG(_request_id ORDER BY _ingested_at DESC LIMIT 1)[OFFSET(0)] AS request_id
FROM {fqn}
WHERE incident_id = @incident_id
  AND _ingested_at >= @p_from
  AND _ingested_at < @p_to
GROUP BY incident_id
LIMIT 1
"""
        params = [
            bigquery.ScalarQueryParameter("incident_id", "STRING", incident_id),
            bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
            bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
        ]
        rows = self.bq.query(sql, params=params)
        steps.append(
            DiagnosisStep(
                step=2,
                name="discovered_ids",
                system="bigquery",
                query=sql.strip(),
                params={
                    "incident_id": incident_id,
                    "p_from": p_from.isoformat(),
                    "p_to": p_to.isoformat(),
                },
                row_count=len(rows),
                result=_jsonable(rows),
                note=(
                    "Empty result means no discovery landing row for this id "
                    "in the partition window."
                ),
            )
        )
        return len(rows) > 0

    def _step_source(
        self, incident_id: str, steps: list[DiagnosisStep]
    ) -> dict[str, Any] | None:
        sql = """
SELECT id, updated_on, created_at, status_status
FROM sentinel_incident
WHERE id = %(incident_id)s
LIMIT %(limit)s
"""
        params = {"incident_id": incident_id, "limit": 1}
        rows = self.sql.fetch_sentinel_mock(sql, params)
        steps.append(
            DiagnosisStep(
                step=3,
                name="source_sentinel_incident",
                system="cloud_sql_source",
                query=sql.strip(),
                params=params,
                row_count=len(rows),
                result=_jsonable(rows),
                note=(
                    "Empty result means the id is absent from the source — "
                    "verdict NOT_AT_SOURCE, not an error."
                ),
            )
        )
        return rows[0] if rows else None

    def _diagnose_discovery_coverage(
        self,
        incident_id: str,
        *,
        updated_on: datetime,
        steps: list[DiagnosisStep],
    ) -> IncidentDiagnosis:
        sql = """
SELECT
  window_id, source, request_id, window_field,
  window_from, window_to, id_count, status, allow_gap, gap_reason,
  started_at, completed_at
FROM discovery_window
WHERE source = %(source)s
  AND window_field = 'updated_on'
  AND window_from <= %(updated_on)s
  AND window_to > %(updated_on)s
ORDER BY started_at DESC
LIMIT %(limit)s
"""
        params = {
            "source": "sentinel",
            "updated_on": _utc(updated_on),
            "limit": SQL_ROW_LIMIT,
        }
        rows = self.sql.fetch_collector(sql, params)
        steps.append(
            DiagnosisStep(
                step=4,
                name="discovery_window_covering_updated_on",
                system="cloud_sql_collector",
                query=sql.strip(),
                params={k: _jsonable(v) for k, v in params.items()},
                row_count=len(rows),
                result=_jsonable(rows),
                note=(
                    "Empty result means no discovery_window row covers this "
                    "updated_on — schedule gap, not a query failure."
                ),
            )
        )

        if not rows:
            gap = self._gap_containing(updated_on, steps)
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="GAP_NOT_SCHEDULED",
                summary=(
                    f"Incident {incident_id} exists at the source "
                    f"(updated_on={_utc(updated_on).isoformat()}) but no "
                    "discovery_window covers that timestamp."
                ),
                steps=steps,
                source_updated_on=_utc(updated_on),
                gap_from=gap.get("gap_from") if gap else None,
                gap_to=gap.get("gap_to") if gap else None,
            )

        statuses = {str(r["status"]) for r in rows}
        if "partial" in statuses:
            partial = next(r for r in rows if r["status"] == "partial")
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="GAP_TRUNCATED",
                summary=(
                    f"A discovery window covering updated_on="
                    f"{_utc(updated_on).isoformat()} has status 'partial' — "
                    "it hit its id_count cap and covered only part of its range. "
                    "The incident was never looked up past the truncation point."
                ),
                steps=steps,
                source_updated_on=_utc(updated_on),
                gap_from=_utc(partial["window_from"]),
                gap_to=_utc(partial["window_to"]),
                covering_window_id=str(partial["window_id"]),
                covering_window_id_count=(
                    int(partial["id_count"])
                    if partial.get("id_count") is not None
                    else None
                ),
            )
        if "failed" in statuses:
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="DISCOVERY_FAILED",
                summary=(
                    f"A discovery window covering updated_on="
                    f"{_utc(updated_on).isoformat()} has status 'failed'."
                ),
                steps=steps,
                source_updated_on=_utc(updated_on),
            )
        if statuses <= {"running"}:
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="DISCOVERY_RUNNING",
                summary=(
                    f"A discovery window covering updated_on="
                    f"{_utc(updated_on).isoformat()} is still 'running'."
                ),
                steps=steps,
                source_updated_on=_utc(updated_on),
            )
        if "complete" in statuses:
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="UNEXPLAINED",
                summary=(
                    f"Incident {incident_id} is at the source, and a complete "
                    "discovery window covers its updated_on, but it is not in "
                    "discovered_ids or incidents_current. It should have been "
                    "found. No invented reason — this needs investigation."
                ),
                steps=steps,
                source_updated_on=_utc(updated_on),
            )
        return IncidentDiagnosis(
            incident_id=incident_id,
            verdict="UNEXPLAINED",
            summary=(
                f"Covering discovery_window rows exist with statuses "
                f"{sorted(statuses)}, but none match a known terminal gap "
                "class."
            ),
            steps=steps,
            source_updated_on=_utc(updated_on),
        )

    def _gap_containing(
        self, updated_on: datetime, steps: list[DiagnosisStep]
    ) -> dict[str, Any] | None:
        """Find a reported coverage gap whose range contains updated_on."""
        sql = """
WITH ordered AS (
  SELECT
    source,
    window_field,
    window_from,
    window_to,
    request_id,
    LEAD(window_from) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_from,
    LEAD(request_id) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_request_id
  FROM discovery_window
  WHERE status = 'complete'
    AND source = %(source)s
    AND window_field = 'updated_on'
),
boundary_gaps AS (
  SELECT
    window_to AS gap_from,
    next_from AS gap_to,
    'not_scheduled' AS reason
  FROM ordered
  WHERE next_from IS NOT NULL
    AND window_to < next_from
)
SELECT gap_from, gap_to, reason
FROM boundary_gaps
WHERE gap_from <= %(updated_on)s
  AND gap_to > %(updated_on)s
ORDER BY gap_from
LIMIT %(limit)s
"""
        params = {
            "source": "sentinel",
            "updated_on": _utc(updated_on),
            "limit": SQL_ROW_LIMIT,
        }
        rows = self.sql.fetch_collector(sql, params)
        steps.append(
            DiagnosisStep(
                step=len(steps) + 1,
                name="gap_containing_updated_on",
                system="cloud_sql_collector",
                query=sql.strip(),
                params={k: _jsonable(v) for k, v in params.items()},
                row_count=len(rows),
                result=_jsonable(rows),
                note=(
                    "Empty means no boundary gap row contains this updated_on "
                    "(e.g. no complete windows exist yet to form boundaries)."
                ),
            )
        )
        if not rows:
            return None
        return {
            "gap_from": _utc(rows[0]["gap_from"]),
            "gap_to": _utc(rows[0]["gap_to"]),
            "reason": rows[0]["reason"],
        }

    def _diagnose_enrichment_gap(
        self, incident_id: str, steps: list[DiagnosisStep]
    ) -> IncidentDiagnosis:
        # page_payload for enrichment is {"incident_ids": ["…", …]}.
        # jsonb ? checks array membership for string elements.
        sql = """
SELECT
  job_id, request_id, source, page_no, status, attempts,
  last_error, created_at, updated_at, loaded_at, record_count
FROM collector_job
WHERE source = %(source)s
  AND page_payload -> 'incident_ids' ? %(incident_id)s
ORDER BY
  CASE status
    WHEN 'dead' THEN 0
    WHEN 'failed' THEN 1
    WHEN 'in_progress' THEN 2
    WHEN 'pending' THEN 3
    WHEN 'done' THEN 4
    ELSE 5
  END,
  updated_at DESC
LIMIT %(limit)s
"""
        params = {
            "source": "sentinel",
            "incident_id": incident_id,
            "limit": SQL_ROW_LIMIT,
        }
        rows = self.sql.fetch_collector(sql, params)
        steps.append(
            DiagnosisStep(
                step=5,
                name="collector_job_for_incident",
                system="cloud_sql_collector",
                query=sql.strip(),
                params=params,
                row_count=len(rows),
                result=_jsonable(rows),
                note=(
                    "Empty result means no enrichment page_payload contains "
                    "this id — discovered but never queued."
                ),
            )
        )

        if not rows:
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="DISCOVERED_NOT_QUEUED",
                summary=(
                    f"Incident {incident_id} is in discovered_ids but no "
                    "collector_job page_payload contains it."
                ),
                steps=steps,
            )

        top = rows[0]
        status = str(top["status"])
        last_error = top.get("last_error")
        if status == "dead":
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="ENRICHMENT_DEAD_LETTERED",
                summary=(
                    f"Enrichment job {top['job_id']} is dead-lettered."
                    + (f" last_error={last_error}" if last_error else "")
                ),
                steps=steps,
                request_id=str(top["request_id"]),
                last_error=last_error,
            )
        if status == "failed":
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="ENRICHMENT_FAILED",
                summary=(
                    f"Enrichment job {top['job_id']} failed."
                    + (f" last_error={last_error}" if last_error else "")
                ),
                steps=steps,
                request_id=str(top["request_id"]),
                last_error=last_error,
            )
        if status == "pending":
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="AWAITING_ENRICHMENT",
                summary=f"Enrichment job {top['job_id']} is pending.",
                steps=steps,
                request_id=str(top["request_id"]),
            )
        if status == "in_progress":
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="IN_PROGRESS",
                summary=f"Enrichment job {top['job_id']} is in_progress.",
                steps=steps,
                request_id=str(top["request_id"]),
            )
        if status == "done":
            return IncidentDiagnosis(
                incident_id=incident_id,
                verdict="ENRICHMENT_DONE_MISSING_WAREHOUSE",
                summary=(
                    f"Enrichment job {top['job_id']} is done but the incident "
                    "is not in incidents_current — reconcile territory."
                ),
                steps=steps,
                request_id=str(top["request_id"]),
            )
        return IncidentDiagnosis(
            incident_id=incident_id,
            verdict="UNEXPLAINED",
            summary=f"Enrichment job {top['job_id']} has unexpected status={status}.",
            steps=steps,
            request_id=str(top["request_id"]),
            last_error=last_error,
        )

    # ------------------------------------------------------------------
    # FUNCTION 2 — time range
    # ------------------------------------------------------------------

    def diagnose_time_range(
        self, range_from: datetime, range_to: datetime
    ) -> RangeDiagnosis:
        range_from = _utc(range_from)
        range_to = _utc(range_to)
        if range_from >= range_to:
            raise ValueError("from must be earlier than to")

        steps: list[DiagnosisStep] = []

        source_count = self._range_source_count(range_from, range_to, steps)
        warehouse_count = self._range_warehouse_count(range_from, range_to, steps)
        discovered_count = self._range_discovered_count(range_from, range_to, steps)
        windows = self._range_windows(range_from, range_to, steps)
        gaps = self._range_gaps(range_from, range_to, steps)
        failed_pages = self._range_failed_pages(range_from, range_to, steps)
        partial_windows = [w for w in windows if w.get("status") == "partial"]

        missing = source_count - warehouse_count
        return RangeDiagnosis(
            range_from=range_from,
            range_to=range_to,
            source_count=source_count,
            warehouse_count=warehouse_count,
            discovered_count=discovered_count,
            missing=missing,
            windows=windows,
            gaps=gaps,
            partial_windows=partial_windows,
            failed_pages=failed_pages,
            steps=steps,
        )

    def _range_source_count(
        self, range_from: datetime, range_to: datetime, steps: list[DiagnosisStep]
    ) -> int:
        sql = """
SELECT count(*)::int AS n
FROM sentinel_incident
WHERE updated_on >= %(from_ts)s
  AND updated_on < %(to_ts)s
"""
        params = {"from_ts": range_from, "to_ts": range_to}
        rows = self.sql.fetch_sentinel_mock(sql, params)
        n = int(rows[0]["n"]) if rows else 0
        steps.append(
            DiagnosisStep(
                step=1,
                name="source_count",
                system="cloud_sql_source",
                query=sql.strip(),
                params={k: _jsonable(v) for k, v in params.items()},
                row_count=len(rows),
                result={"count": n},
            )
        )
        return n

    def _range_warehouse_count(
        self, range_from: datetime, range_to: datetime, steps: list[DiagnosisStep]
    ) -> int:
        p_from, p_to = _partition_bounds(range_from=range_from, range_to=range_to)
        fqn = (
            f"`{self.settings.gcp_project}."
            f"{self.settings.bq_core_dataset}.incidents_current`"
        )
        # Thread-exploded + append-only: never count(*).
        sql = f"""
SELECT COUNT(DISTINCT id) AS n
FROM {fqn}
WHERE updatedOn >= @from_ts
  AND updatedOn < @to_ts
  AND _ingested_at >= @p_from
  AND _ingested_at < @p_to
"""
        params = [
            bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", range_from),
            bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", range_to),
            bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
            bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
        ]
        rows = self.bq.query(sql, params=params)
        n = int(rows[0]["n"]) if rows else 0
        steps.append(
            DiagnosisStep(
                step=2,
                name="warehouse_count_distinct_id",
                system="bigquery",
                query=sql.strip(),
                params={
                    "from_ts": range_from.isoformat(),
                    "to_ts": range_to.isoformat(),
                    "p_from": p_from.isoformat(),
                    "p_to": p_to.isoformat(),
                },
                row_count=len(rows),
                result={"count": n},
                note="COUNT(DISTINCT id) — export is thread-exploded (~2.5×).",
            )
        )
        return n

    def _range_discovered_count(
        self, range_from: datetime, range_to: datetime, steps: list[DiagnosisStep]
    ) -> int:
        p_from, p_to = _partition_bounds(range_from=range_from, range_to=range_to)
        fqn = (
            f"`{self.settings.gcp_project}."
            f"{self.settings.bq_raw_dataset}.discovered_ids`"
        )
        sql = f"""
SELECT COUNT(DISTINCT incident_id) AS n
FROM {fqn}
WHERE COALESCE(discovered_at, _ingested_at) >= @from_ts
  AND COALESCE(discovered_at, _ingested_at) < @to_ts
  AND _ingested_at >= @p_from
  AND _ingested_at < @p_to
"""
        params = [
            bigquery.ScalarQueryParameter("from_ts", "TIMESTAMP", range_from),
            bigquery.ScalarQueryParameter("to_ts", "TIMESTAMP", range_to),
            bigquery.ScalarQueryParameter("p_from", "TIMESTAMP", p_from),
            bigquery.ScalarQueryParameter("p_to", "TIMESTAMP", p_to),
        ]
        rows = self.bq.query(sql, params=params)
        n = int(rows[0]["n"]) if rows else 0
        steps.append(
            DiagnosisStep(
                step=3,
                name="discovered_count_distinct_id",
                system="bigquery",
                query=sql.strip(),
                params={
                    "from_ts": range_from.isoformat(),
                    "to_ts": range_to.isoformat(),
                    "p_from": p_from.isoformat(),
                    "p_to": p_to.isoformat(),
                },
                row_count=len(rows),
                result={"count": n},
            )
        )
        return n

    def _range_windows(
        self, range_from: datetime, range_to: datetime, steps: list[DiagnosisStep]
    ) -> list[dict[str, Any]]:
        sql = """
SELECT
  window_id, source, request_id, window_field,
  window_from, window_to, id_count, status, allow_gap, gap_reason,
  started_at, completed_at
FROM discovery_window
WHERE source = %(source)s
  AND window_field = 'updated_on'
  AND window_from < %(to_ts)s
  AND window_to > %(from_ts)s
ORDER BY window_from
LIMIT %(limit)s
"""
        params = {
            "source": "sentinel",
            "from_ts": range_from,
            "to_ts": range_to,
            "limit": SQL_ROW_LIMIT,
        }
        rows = self.sql.fetch_collector(sql, params)
        out = _jsonable(rows)
        steps.append(
            DiagnosisStep(
                step=4,
                name="overlapping_discovery_windows",
                system="cloud_sql_collector",
                query=sql.strip(),
                params={k: _jsonable(v) for k, v in params.items()},
                row_count=len(rows),
                result=out,
            )
        )
        return out  # type: ignore[return-value]

    def _range_gaps(
        self, range_from: datetime, range_to: datetime, steps: list[DiagnosisStep]
    ) -> list[dict[str, Any]]:
        # Same semantics as sql/011_discovery_gaps.sql, then filter overlap.
        sql = """
WITH ordered AS (
  SELECT
    source,
    window_field,
    window_from,
    window_to,
    request_id,
    LEAD(window_from) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_from,
    LEAD(request_id) OVER (
      PARTITION BY source, window_field
      ORDER BY window_from
    ) AS next_request_id
  FROM discovery_window
  WHERE status = 'complete'
),
boundary_gaps AS (
  SELECT
    source,
    window_field,
    window_to AS gap_from,
    next_from AS gap_to,
    next_from - window_to AS gap_duration,
    request_id AS before_request_id,
    next_request_id AS after_request_id,
    'not_scheduled' AS reason,
    false AS uncertain
  FROM ordered
  WHERE next_from IS NOT NULL
    AND window_to < next_from
),
truncated_gaps AS (
  SELECT
    source,
    window_field,
    window_from AS gap_from,
    window_to AS gap_to,
    window_to - window_from AS gap_duration,
    request_id AS before_request_id,
    NULL::uuid AS after_request_id,
    'truncated' AS reason,
    true AS uncertain
  FROM discovery_window
  WHERE status = 'partial'
),
all_gaps AS (
  SELECT * FROM boundary_gaps
  UNION ALL
  SELECT * FROM truncated_gaps
)
SELECT *
FROM all_gaps
WHERE gap_from < %(to_ts)s
  AND gap_to > %(from_ts)s
ORDER BY source, window_field, gap_from
LIMIT %(limit)s
"""
        params = {
            "from_ts": range_from,
            "to_ts": range_to,
            "limit": SQL_ROW_LIMIT,
        }
        rows = self.sql.fetch_collector(sql, params)
        out = _jsonable(rows)
        steps.append(
            DiagnosisStep(
                step=5,
                name="discovery_gaps_overlapping_range",
                system="cloud_sql_collector",
                query=sql.strip(),
                params={k: _jsonable(v) for k, v in params.items()},
                row_count=len(rows),
                result=out,
                note=(
                    "Empty gaps list means the gap query returned no rows for "
                    "this range — not that coverage is complete (there may be "
                    "no windows at all)."
                ),
            )
        )
        return out  # type: ignore[return-value]

    def _range_failed_pages(
        self, range_from: datetime, range_to: datetime, steps: list[DiagnosisStep]
    ) -> list[dict[str, Any]]:
        sql = """
SELECT
  job_id, request_id, source, page_no, status, attempts,
  last_error, created_at, updated_at
FROM collector_job
WHERE source IN ('sentinel', 'sentinel_discovery')
  AND status IN ('dead', 'failed')
  AND created_at >= %(from_ts)s
  AND created_at < %(to_ts)s
ORDER BY created_at
LIMIT %(limit)s
"""
        params = {
            "from_ts": range_from,
            "to_ts": range_to,
            "limit": SQL_ROW_LIMIT,
        }
        rows = self.sql.fetch_collector(sql, params)
        out = _jsonable(rows)
        steps.append(
            DiagnosisStep(
                step=6,
                name="failed_or_dead_collector_jobs",
                system="cloud_sql_collector",
                query=sql.strip(),
                params={k: _jsonable(v) for k, v in params.items()},
                row_count=len(rows),
                result=out,
            )
        )
        return out  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # FUNCTION 3 — explain a gap range
    # ------------------------------------------------------------------

    def explain_gap(self, gap_from: datetime, gap_to: datetime) -> GapExplanation:
        gap_from = _utc(gap_from)
        gap_to = _utc(gap_to)
        if gap_from >= gap_to:
            raise ValueError("from must be earlier than to")

        steps: list[DiagnosisStep] = []
        windows = self._range_windows(gap_from, gap_to, steps)
        # Re-number steps for this function's narrative.
        for i, step in enumerate(steps, start=1):
            step.step = i

        gaps = self._range_gaps(gap_from, gap_to, steps)
        for i, step in enumerate(steps, start=1):
            step.step = i

        failed_sql = """
SELECT
  window_id, source, request_id, window_field,
  window_from, window_to, id_count, status, gap_reason,
  started_at, completed_at
FROM discovery_window
WHERE source = %(source)s
  AND window_field = 'updated_on'
  AND status = 'failed'
  AND window_from < %(to_ts)s
  AND window_to > %(from_ts)s
ORDER BY window_from
LIMIT %(limit)s
"""
        failed_params = {
            "source": "sentinel",
            "from_ts": gap_from,
            "to_ts": gap_to,
            "limit": SQL_ROW_LIMIT,
        }
        failed_windows = _jsonable(
            self.sql.fetch_collector(failed_sql, failed_params)
        )
        steps.append(
            DiagnosisStep(
                step=len(steps) + 1,
                name="failed_discovery_windows",
                system="cloud_sql_collector",
                query=failed_sql.strip(),
                params={k: _jsonable(v) for k, v in failed_params.items()},
                row_count=len(failed_windows),
                result=failed_windows,
            )
        )

        hb_sql = """
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
        hb_params = {
            "from_ts": gap_from,
            "to_ts": gap_to,
            "limit": SQL_ROW_LIMIT,
        }
        # Table only stores current workers — not a historical log. An empty
        # result means "no worker rows with heartbeats near this window now",
        # not proof that none ran then.
        heartbeats = _jsonable(self.sql.fetch_collector(hb_sql, hb_params))
        steps.append(
            DiagnosisStep(
                step=len(steps) + 1,
                name="procrastinate_worker_heartbeats",
                system="cloud_sql_collector",
                query=hb_sql.strip(),
                params={k: _jsonable(v) for k, v in hb_params.items()},
                row_count=len(heartbeats),
                result=heartbeats,
                note=(
                    "procrastinate_workers holds current workers only. Empty "
                    "here means no live/recent heartbeat rows, not a historical "
                    "absence proof — cross-check Cloud Run executions."
                ),
            )
        )

        executions: list[dict[str, Any]] = []
        if self.gcp is not None:
            for job_name in self.settings.cloud_run_jobs:
                try:
                    for ex in self.gcp.list_executions(job_name, page_size=20):
                        executions.append({"job": job_name, **ex})
                except Exception as exc:  # noqa: BLE001
                    steps.append(
                        DiagnosisStep(
                            step=len(steps) + 1,
                            name=f"cloud_run_executions_{job_name}",
                            system="cloud_run",
                            query=f"ExecutionsClient.list_executions({job_name})",
                            params={"job": job_name},
                            row_count=0,
                            result=None,
                            note=f"list_executions failed: {exc}",
                        )
                    )
            steps.append(
                DiagnosisStep(
                    step=len(steps) + 1,
                    name="cloud_run_executions",
                    system="cloud_run",
                    query="ExecutionsClient.list_executions for col-* jobs",
                    params={"jobs": list(self.settings.cloud_run_jobs)},
                    row_count=len(executions),
                    result=executions[:50],
                )
            )

        cause, summary = self._classify_gap_cause(
            windows=windows,
            gaps=gaps,
            failed_windows=failed_windows,  # type: ignore[arg-type]
            heartbeats=heartbeats,  # type: ignore[arg-type]
            executions=executions,
            gap_from=gap_from,
            gap_to=gap_to,
        )
        return GapExplanation(
            gap_from=gap_from,
            gap_to=gap_to,
            cause=cause,
            summary=summary,
            windows=windows,
            gaps=gaps,
            failed_windows=failed_windows,  # type: ignore[return-value]
            worker_heartbeats=heartbeats,  # type: ignore[return-value]
            cloud_run_executions=executions,
            steps=steps,
        )

    @staticmethod
    def _classify_gap_cause(
        *,
        windows: list[dict[str, Any]],
        gaps: list[dict[str, Any]],
        failed_windows: list[dict[str, Any]],
        heartbeats: list[dict[str, Any]],
        executions: list[dict[str, Any]],
        gap_from: datetime,
        gap_to: datetime,
    ) -> tuple[GapCause, str]:
        reasons = {str(g.get("reason")) for g in gaps}
        partial = [w for w in windows if w.get("status") == "partial"]
        signals: list[GapCause] = []

        if "truncated" in reasons or partial:
            signals.append("truncated")
        if failed_windows:
            signals.append("failed_discovery")
        if not windows:
            signals.append("never_scheduled")
        elif "not_scheduled" in reasons:
            signals.append("never_scheduled")

        # Workers: empty heartbeats + no overlapping Cloud Run executions.
        if not heartbeats and not executions:
            signals.append("no_workers")

        # Deduplicate while preserving order.
        ordered: list[GapCause] = []
        for s in signals:
            if s not in ordered:
                ordered.append(s)

        if not ordered:
            return (
                "unknown",
                (
                    f"Gap {gap_from.isoformat()} → {gap_to.isoformat()}: "
                    "windows exist and no truncated/failed/not_scheduled signal "
                    "matched; investigate manually."
                ),
            )
        if len(ordered) == 1:
            cause = ordered[0]
        else:
            cause = "mixed"

        parts = []
        if "never_scheduled" in ordered:
            parts.append("no complete discovery window covered the range")
        if "truncated" in ordered:
            parts.append("at least one window truncated at the id_count cap")
        if "failed_discovery" in ordered:
            parts.append("at least one discovery window failed")
        if "no_workers" in ordered:
            parts.append(
                "no procrastinate_workers heartbeats and no Cloud Run executions "
                "visible for the period"
            )
        return (
            cause,
            f"Gap {gap_from.isoformat()} → {gap_to.isoformat()}: " + "; ".join(parts) + ".",
        )
