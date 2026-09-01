"""Request API that LiSN calls.

These counts come from OUR table, never from procrastinate_jobs. Reading a
library's internal schema would pin LiSN's API to a version we do not control.

Operational endpoints (/v1/reconcile, /v1/dead-letter, /v1/health/detail) also
read Procrastinate's tables. LiSN's public counts endpoint reads only ours.
These three are operational.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any, Self

# OTel before any other collector import so instrumentors patch libraries
# (FastAPI / httpx / psycopg) before those modules are pulled in and used.
from collector.telemetry import init_telemetry

init_telemetry()

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.cloud import bigquery
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic.types import StrictStr

from collector.admin_reset import (
    RESET_BQ_TABLES,
    RESET_CLOUD_SQL_TABLES,
    RESET_CONFIRM_PHRASE,
    ResetInProgressError,
    admin_reset_enabled,
    collect_live_state,
    run_reset,
)
from collector.app import app as procrastinate_app
from collector.db import connect
from collector.discovery_gaps import (
    axes_from_query_spec,
    check_submit_contiguity,
    estimate_truncation_risk,
    gap_summary,
    insert_discovery_windows,
    list_gaps,
    partial_windows_summary,
)
from collector.logging_setup import get_logger, log
from collector.metrics import record_request_pages, record_request_received
from collector.redact import redact_secrets
from collector.shortfall import requested_count as page_requested_count
from collector.sources import get
from collector.tasks import fetch_page
from collector.tracing import (
    inject_trace_context,
    key_type_and_count,
    traced_span,
)

api = FastAPI(title="LiSN Collectors Request API")
logger = get_logger(__name__)

# Demo frontend (make frontend → :3000) calls this API on :8080. Tokens still
# must not live in the browser — use gcloud run services proxy for Cloud Run.
api.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# HARD ALLOWLIST — only these may ever be cleared by
# DELETE /v1/admin/collector-data (or the deprecated POST /v1/admin/reset alias).
# Never discover tables dynamically, never pattern-match on names, never
# iterate information_schema. Re-exported from admin_reset for visibility here.
ADMIN_RESET_CLOUD_SQL_ALLOWLIST: list[str] = list(RESET_CLOUD_SQL_TABLES)
ADMIN_RESET_BQ_ALLOWLIST: list[str] = list(RESET_BQ_TABLES)


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _row_dict(columns: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    return {col: _jsonable(val) for col, val in zip(columns, row, strict=True)}


# Pydantic 2.13 (via fastapi==0.115.*) already rejects a bare str for list[str]
# without full-model strict=True. Full-model strict breaks ISO datetime strings
# on DiscoveryQuery. StrictStr on list elements blocks int→str coercion; verified
# that incident_ids="IN2608" and incident_ids=[1] both raise.
class SentinelKeyQuery(BaseModel):
    """Enrichment query: exactly one of the three key lists."""

    model_config = ConfigDict(extra="forbid")
    # extra=forbid: a caller sending a field we ignore believes it took effect.

    incident_ids: list[StrictStr] | None = None
    order_item_ids: list[StrictStr] | None = None
    order_ids: list[StrictStr] | None = None

    @model_validator(mode="after")
    def exactly_one_key_list(self) -> Self:
        supplied: list[str] = []
        if self.incident_ids:
            supplied.append("incident_ids")
        if self.order_item_ids:
            supplied.append("order_item_ids")
        if self.order_ids:
            supplied.append("order_ids")
        if len(supplied) > 1:
            raise ValueError(
                "exactly one of incident_ids, order_item_ids, order_ids required; "
                f"got {', '.join(supplied)}"
            )
        if not supplied:
            raise ValueError(
                "incident_ids, order_item_ids or order_ids required — no generic queries"
            )
        return self


class DiscoveryQuery(BaseModel):
    """Discovery filter query (sentinel_discovery)."""

    model_config = ConfigDict(extra="forbid")
    # extra=forbid: a caller sending a field we ignore believes it took effect
    # (e.g. order_item_ids on a discovery request must 400, not be dropped).

    updated_from: datetime | None = None
    updated_to: datetime | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    statuses: list[StrictStr] | None = None
    issue_names: list[StrictStr] | None = None
    limit: int = 1000

    @field_validator("limit")
    @classmethod
    def limit_range(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"limit must be an int, got {value!r}")
        if value < 1 or value > 5000:
            raise ValueError(f"limit must be between 1 and 5000, got {value}")
        return value

    @model_validator(mode="after")
    def window_bounds_strict(self) -> Self:
        # discovery_window CHECK (window_from < window_to) — reject equals too.
        for start, end, label in (
            (self.updated_from, self.updated_to, "updated"),
            (self.created_from, self.created_to, "created"),
        ):
            if start is not None and end is not None and not start < end:
                raise ValueError(
                    f"{label}_from must be strictly before {label}_to"
                )
        return self


class CollectBody(BaseModel):
    source: str
    query_spec: SentinelKeyQuery | DiscoveryQuery
    # Discovery only: REJECT BY DEFAULT when the new window skips past the
    # latest completed window_to. A scheduler misfire should be loud — this is
    # the difference between the collector silently doing what it was told and
    # the collector telling you that what you asked for skips an hour.
    allow_gap: bool = False
    gap_reason: str | None = None
    # Queue order only (Procrastinate priority DESC). Default 0.
    # Does NOT bypass min_interval_s, does NOT preempt in-flight pages — the
    # realistic gain is waiting for the next free worker instead of the whole
    # backlog. The fast lane only works while it stays short (see flood guard).
    priority: int = Field(default=0, ge=0, le=10)

    @model_validator(mode="before")
    @classmethod
    def bind_typed_query_spec(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        source = data.get("source")
        raw = data.get("query_spec", {})
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("query_spec must be an object")
        if source == "sentinel":
            return {**data, "query_spec": SentinelKeyQuery.model_validate(raw)}
        if source == "sentinel_discovery":
            return {**data, "query_spec": DiscoveryQuery.model_validate(raw)}
        # Unknown source: still type as key-query shape so loose dicts cannot
        # slip through; get() then raises ValueError naming known sources.
        return {**data, "query_spec": SentinelKeyQuery.model_validate(raw)}

    @model_validator(mode="after")
    def gap_reason_required_when_allowing(self) -> Self:
        if self.allow_gap and self.source == "sentinel_discovery":
            if not (self.gap_reason and str(self.gap_reason).strip()):
                raise ValueError(
                    "gap_reason is required when allow_gap=true "
                    "(say why the skipped range is intentional)"
                )
        return self


class AdminResetBody(BaseModel):
    confirm: str
    # dry_run defaults TRUE — callers must explicitly ask for a real reset.
    dry_run: bool = True
    # Resetting mid-run leaves workers writing into emptied tables.
    force: bool = False


def _validation_detail(errors: list[dict[str, Any]]) -> str:
    """Flatten Pydantic/FastAPI errors into one message that names fields."""
    parts: list[str] = []
    for err in errors:
        loc = err.get("loc") or ()
        # Drop the "body" prefix FastAPI adds.
        path = ".".join(str(x) for x in loc if x != "body")
        msg = str(err.get("msg") or "invalid")
        parts.append(f"{path}: {msg}" if path else msg)
    return "; ".join(parts) if parts else "invalid request"


@api.exception_handler(RequestValidationError)
async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # /v1/collect must stay on 400 (LiSN + acceptance tests); other routes keep
    # FastAPI's default 422.
    if request.method == "POST" and request.url.path.rstrip("/") == "/v1/collect":
        return JSONResponse(
            status_code=400,
            content={"detail": _validation_detail(list(exc.errors()))},
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


def _caller_identity(request: Request) -> str:
    return (
        request.headers.get("x-goog-authenticated-user-email")
        or request.headers.get("x-forwarded-email")
        or (request.client.host if request.client else "unknown")
    )


def _page_key_count(payload: dict[str, Any]) -> int:
    return page_requested_count(payload)


def _query_spec_dict(query: SentinelKeyQuery | DiscoveryQuery) -> dict[str, Any]:
    return query.model_dump(mode="json", exclude_none=True)


@api.post(
    "/v1/collect",
    summary="Accept a collect or discovery request",
    description=(
        "Splits the query into pages, stores collector_job rows, and defers "
        "Procrastinate work.\n\n"
        "**priority** (0–10, default 0) affects QUEUE ORDER only "
        "(Procrastinate `ORDER BY priority DESC`). It does NOT bypass the "
        "per-source rate limit: an urgent page still waits for a free worker "
        "slot and still sleeps `min_interval_s`. It does NOT preempt a page "
        "already in flight. The realistic gain is waiting for the next free "
        "worker instead of the whole backlog. Priority above 5 is rejected "
        "when the request expands to more than 20 pages — the fast lane only "
        "works while it stays short."
    ),
)
def collect(body: CollectBody) -> dict[str, Any]:
    query_spec = _query_spec_dict(body.query_spec)
    overlap_warnings: list[str] = []
    truncation_warnings: list[dict[str, Any]] = []

    # Discovery submit-time contiguity guard — before plan/insert.
    if body.source == "sentinel_discovery":
        for axis in axes_from_query_spec(query_spec):
            check = check_submit_contiguity(source=body.source, axis=axis)
            if check.gap_from is not None and check.gap_to is not None:
                # REJECT BY DEFAULT. allow_gap=true is the explicit override.
                if not body.allow_gap:
                    dur = check.gap_duration or (check.gap_to - check.gap_from)
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "message": (
                                "discovery window skips past the latest completed "
                                f"{check.field} window — refusing by default so a "
                                "scheduler misfire is loud"
                            ),
                            "source": body.source,
                            "window_field": check.field,
                            "gap_from": check.gap_from.isoformat(),
                            "gap_to": check.gap_to.isoformat(),
                            "gap_duration": str(dur),
                            "gap_duration_seconds": dur.total_seconds(),
                            "hint": (
                                "pass allow_gap=true with gap_reason to accept; "
                                "gaps are reported on /v1/discovery/gaps and are "
                                "never auto-backfilled"
                            ),
                        },
                    )
                log(
                    logger,
                    logging.CRITICAL,
                    "DISCOVERY GAP DETECTED — accepting with allow_gap",
                    source=body.source,
                    status="discovery_gap",
                    duration_ms=int(
                        (
                            check.gap_duration
                            or (check.gap_to - check.gap_from)
                        ).total_seconds()
                        * 1000
                    ),
                )
            if check.overlap_with_to is not None:
                # Overlap is not a gap — re-collection is safe and sometimes
                # deliberate. Surface as a warning; do not reject.
                overlap_warnings.append(
                    f"{check.field} window starts before latest completed "
                    f"window_to={check.overlap_with_to.isoformat()} "
                    "(overlap; not rejected)"
                )
                log(
                    logger,
                    logging.WARNING,
                    "discovery window overlaps the previous one",
                    source=body.source,
                    status="overlap",
                )
            risk = estimate_truncation_risk(
                source=body.source, axis=axis, query_spec=query_spec
            )
            if risk is not None:
                truncation_warnings.append(risk)

    key_type, key_count = key_type_and_count(query_spec, body.source)
    with traced_span(
        "collect_request",
        attributes={
            "source": body.source,
            "key_type": key_type,
            "key_count": key_count,
            "priority": body.priority,
            "lisn.source": body.source,
            "lisn.key_type": key_type,
            "lisn.key_count": key_count,
            "lisn.priority": body.priority,
        },
    ) as root_span:
        try:
            with traced_span("plan_pages"):
                src = get(body.source)
                pages = src.plan(query_spec)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=redact_secrets(str(exc))
            ) from exc

        root_span.set_attribute("page_count", len(pages))
        root_span.set_attribute("lisn.page_count", len(pages))

        # Flood guard: priority > 5 with a large page fan-out recreates the
        # backlog problem with extra steps. The fast lane only works while it
        # stays short (~20 pages / 1,000 keys at batch_cap=50).
        if body.priority > 5 and len(pages) > 20:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"priority {body.priority} rejected for {len(pages)} pages — "
                    "the fast lane is for small urgent requests "
                    "(priority > 5 requires ≤ 20 pages / ~1000 keys)"
                ),
            )

        # plan() runs HERE, once, at request time. It is never called again.
        # Recovery re-reads the stored page_payload rather than recomputing, so a
        # changed underlying dataset can never shift the page boundaries mid-request.

        request_id = uuid.uuid4()
        job_ids: list[uuid.UUID] = []

        # Capture W3C context WHILE collect_request is current. Workers extract
        # this later — Postgres is the only hop between API and worker.
        trace_carrier = inject_trace_context()

        # Rows are written BEFORE jobs are deferred. If the process dies between the
        # two, the sweeper finds orphan rows — the safe direction. Deferring first
        # would queue jobs pointing at rows that do not exist.
        with traced_span(
            "write_job_rows",
            attributes={"lisn.page_count": len(pages)},
        ):
            with connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO collector_request (
                          request_id, source, query_spec, total_pages, status
                        )
                        VALUES (%s, %s, %s::jsonb, %s, 'open')
                        """,
                        (
                            request_id,
                            body.source,
                            json.dumps(query_spec),
                            len(pages),
                        ),
                    )
                    for page in pages:
                        job_id = uuid.uuid4()
                        job_ids.append(job_id)
                        cur.execute(
                            """
                            INSERT INTO collector_job (
                              job_id, request_id, source, page_no, page_payload,
                              status, requested_count, priority, trace_context
                            )
                            VALUES (
                              %s, %s, %s, %s, %s::jsonb, 'pending', %s, %s, %s
                            )
                            """,
                            (
                                job_id,
                                request_id,
                                body.source,
                                page.page_no,
                                json.dumps(page.payload),
                                page_requested_count(page.payload),
                                body.priority,
                                trace_carrier,
                            ),
                        )
                conn.commit()

        if body.source == "sentinel_discovery":
            insert_discovery_windows(
                request_id=request_id,
                source=body.source,
                query_spec=query_spec,
                allow_gap=body.allow_gap,
                gap_reason=body.gap_reason,
            )

        with traced_span(
            "defer_jobs",
            attributes={"lisn.page_count": len(job_ids)},
        ):
            with procrastinate_app.open():
                for job_id in job_ids:
                    # Queue name == source name (contract). Discovery and enrichment
                    # workers listen on different queues; defer must match.
                    # priority → procrastinate_jobs.priority (ORDER BY priority DESC).
                    fetch_page.configure(
                        queue=body.source, priority=body.priority
                    ).defer(job_id=str(job_id))

        total_keys = sum(_page_key_count(page.payload) for page in pages)
        out: dict[str, Any] = {
            "request_id": str(request_id),
            "total_pages": len(pages),
            "keys": total_keys,
            "priority": body.priority,
        }
        if overlap_warnings:
            out["warnings"] = overlap_warnings
        if truncation_warnings:
            out["truncation_warnings"] = truncation_warnings
        if body.allow_gap and body.source == "sentinel_discovery":
            out["allow_gap"] = True
            out["gap_reason"] = body.gap_reason
        log(
            logger,
            logging.INFO,
            "request accepted",
            request_id=str(request_id),
            source=body.source,
            status="accepted",
            record_count=len(pages),
            requested_count=total_keys,
        )
        record_request_received(source=body.source, key_type=key_type)
        record_request_pages(source=body.source, page_count=len(pages))
        return out


@api.get("/v1/requests/{request_id}/counts")
def request_counts(request_id: str) -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, count(*)::int
                FROM collector_job
                WHERE request_id = %s::uuid
                GROUP BY status
                """,
                (request_id,),
            )
            counts = {status: count for status, count in cur.fetchall()}
            cur.execute(
                """
                SELECT
                  coalesce(sum(record_count), 0)::int,
                  coalesce(sum(requested_count), 0)::int,
                  coalesce(sum(returned_count), 0)::int
                FROM collector_job
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            records, requested, returned = cur.fetchone()
            records = int(records)
            requested = int(requested)
            returned = int(returned)
    # missing = shortfall of distinct source entities vs keys asked.
    # A shortfall is not necessarily an error — a key can legitimately not
    # exist. It is an ANOMALY worth surfacing, not a failure worth alerting on.
    return {
        "request_id": request_id,
        "counts": counts,
        "records": records,
        "requested": requested,
        "returned": returned,
        "missing": requested - returned,
    }


@api.get(
    "/v1/requests/{request_id}/results",
    summary="Concrete landing proof for one collect request",
)
def request_results(request_id: str) -> dict[str, Any]:
    """Pages / GCS / BigQuery / unloaded for a single request — not inferred."""
    project = os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    region = os.environ.get("REGION", "asia-south1")

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, total_pages
                FROM collector_request
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="request not found")
            source, total_pages = row[0], int(row[1])

            cur.execute(
                """
                SELECT status, count(*)::int
                FROM collector_job
                WHERE request_id = %s::uuid
                GROUP BY status
                """,
                (request_id,),
            )
            by_status = {status: count for status, count in cur.fetchall()}
            done = int(by_status.get("done", 0))

            cur.execute(
                """
                SELECT count(*)::int
                FROM raw_manifest m
                JOIN collector_job j ON j.job_id = m.job_id
                WHERE j.request_id = %s::uuid
                """,
                (request_id,),
            )
            gcs_objects = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE request_id = %s::uuid
                  AND raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                """,
                (request_id,),
            )
            unloaded = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT page_no, status, owner, record_count,
                       raw_written_at, loaded_at
                FROM collector_job
                WHERE request_id = %s::uuid
                ORDER BY page_no
                LIMIT 5
                """,
                (request_id,),
            )
            recent_jobs = [
                {
                    "page_no": r[0],
                    "status": r[1],
                    "owner": r[2],
                    "record_count": r[3],
                    "raw_written_at": r[4].isoformat() if r[4] else None,
                    "loaded_at": r[5].isoformat() if r[5] else None,
                }
                for r in cur.fetchall()
            ]

    bq_rows: int | None = None
    bq_distinct: int | None = None
    bq_table: str | None = None
    if project:
        if source == "sentinel":
            # Must match SentinelSource.bq_table — loads land in incidents_v2
            # (STRING ids); the legacy FLOAT64 incidents table is not serving.
            bq_table = f"{project}.sentinel_raw.incidents_v2"
            id_col = "id"
        elif source == "sentinel_discovery":
            bq_table = f"{project}.sentinel_raw.discovered_ids"
            id_col = "incident_id"
        else:
            bq_table = None
            id_col = "id"
        if bq_table:
            client = bigquery.Client(project=project, location=region)
            try:
                qrow = list(
                    client.query(
                        f"""
                        SELECT count(*) AS n,
                               count(DISTINCT {id_col}) AS d
                        FROM `{bq_table}`
                        WHERE _request_id = @rid
                        """,
                        job_config=bigquery.QueryJobConfig(
                            query_parameters=[
                                bigquery.ScalarQueryParameter(
                                    "rid", "STRING", request_id
                                ),
                            ]
                        ),
                        location=region,
                    ).result()
                )[0]
                bq_rows = int(qrow.n)
                bq_distinct = int(qrow.d)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=502,
                    detail=f"BigQuery results failed: {redact_secrets(str(exc))}",
                ) from exc

    return {
        "request_id": request_id,
        "source": source,
        "pages": {"done": done, "total": total_pages, "by_status": by_status},
        "gcs_objects": gcs_objects,
        "bigquery_rows": bq_rows,
        "bigquery_distinct": bq_distinct,
        "bigquery_table": bq_table,
        "unloaded": unloaded,
        "recent_jobs": recent_jobs,
    }


@api.get(
    "/v1/discovered/pending",
    summary="Discovered IDs not yet in incidents_current (bridge)",
    description=(
        "Runs the LiSN bridge query (sql/008_discovery_to_enrich.sql): ids in "
        "discovered_ids_latest that are absent from incidents_current. Used by "
        "the demo UI Stage 2 before enrichment."
    ),
)
def discovered_pending(
    limit: int = Query(1000, ge=1, le=50_000),
) -> dict[str, Any]:
    project = os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise HTTPException(status_code=500, detail="PROJECT unset")
    region = os.environ.get("REGION", "asia-south1")
    # Same seam as sql/008_discovery_to_enrich.sql — business decision stays in SQL.
    bridge_from = f"""
        FROM `{project}.sentinel_core.discovered_ids_latest` AS d
        LEFT JOIN `{project}.sentinel_core.incidents_current` AS i
          ON i.id = d.incident_id
        WHERE i.id IS NULL
    """
    client = bigquery.Client(project=project, location=region)
    try:
        pending_total = int(
            list(
                client.query(
                    f"SELECT count(*) AS n {bridge_from}",
                    location=region,
                ).result()
            )[0].n
        )
        rows = list(
            client.query(
                f"""
                SELECT d.incident_id
                {bridge_from}
                ORDER BY d.incident_id
                LIMIT @limit
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("limit", "INT64", limit),
                    ]
                ),
                location=region,
            ).result()
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=(
                f"discovered/pending BigQuery failed: "
                f"{redact_secrets(str(exc))}"
            ),
        ) from exc

    ids = [str(row.incident_id) for row in rows if row.incident_id is not None]
    return {
        "ids": ids,
        "returned": len(ids),
        "pending_total": pending_total,
        "limit": limit,
    }


@api.get("/v1/counts")
def global_counts() -> dict[str, Any]:
    # This is why all collectors share one collector_job table — LiSN asking
    # for open/in-progress/closed across every source must be one query, not six.
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, status, count(*)::int
                FROM collector_job
                GROUP BY source, status
                ORDER BY source, status
                """
            )
            counts: dict[str, dict[str, int]] = {}
            for source, status, count in cur.fetchall():
                counts.setdefault(source, {})[status] = count
    return {"counts": counts}


@api.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# WHY reconcile exists, and why review called it non-negotiable: a page can
# succeed at writing raw JSON to GCS and then the process dies before the
# BigQuery insert. That failure produces no error anywhere. Every other failure
# mode is loud; this one is silent. The 15-minute grace period exists so a page
# currently mid-flight is not reported as a gap.
@api.get("/v1/reconcile")
def reconcile(minutes: int = Query(default=15, ge=0)) -> dict[str, Any]:
    columns = (
        "job_id",
        "source",
        "request_id",
        "page_no",
        "raw_uri",
        "raw_written_at",
        "attempts",
    )
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, request_id, page_no, raw_uri,
                       raw_written_at, attempts
                FROM collector_job
                WHERE raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                  AND raw_written_at < now() - make_interval(mins => %s)
                ORDER BY raw_written_at
                """,
                (minutes,),
            )
            rows = [_row_dict(columns, row) for row in cur.fetchall()]
    if rows:
        log(
            logger,
            logging.CRITICAL,
            "reconcile found raw-without-load rows",
            status="reconcile_unloaded",
            record_count=len(rows),
        )
        for row in rows[:20]:
            log(
                logger,
                logging.CRITICAL,
                "reconcile unloaded row",
                request_id=str(row.get("request_id")),
                job_id=str(row.get("job_id")),
                source=row.get("source"),
                page_no=row.get("page_no"),
                attempt=row.get("attempts"),
                status="reconcile_unloaded",
            )
    return {"unloaded": len(rows), "rows": rows}


# These need a human. They exhausted their attempts and nothing will retry
# them automatically.
@api.get("/v1/dead-letter")
def dead_letter() -> dict[str, Any]:
    columns = (
        "job_id",
        "source",
        "request_id",
        "page_no",
        "attempts",
        "last_error",
        "updated_at",
    )
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, request_id, page_no, attempts,
                       last_error, updated_at
                FROM collector_job
                WHERE status = 'dead'
                ORDER BY updated_at
                """
            )
            rows = [_row_dict(columns, row) for row in cur.fetchall()]
    return {"dead": len(rows), "rows": rows}


@api.get(
    "/v1/discovery/gaps",
    summary="Coverage gaps between discovery windows",
    description=(
        "Lists ranges where discovery coverage is incomplete "
        "(sql/011_discovery_gaps.sql). Boundary gaps (reason=not_scheduled) are "
        "holes between completed windows. Truncated gaps (reason=truncated, "
        "uncertain=true) are partial windows that stopped at the ID cap. Failed "
        "and running windows are excluded from the boundary chain. "
        "This endpoint REPORTS gaps only; it does not collect or backfill them."
    ),
)
def discovery_gaps(
    source: str | None = Query(None, description="Filter by source name"),
    range_from: datetime | None = Query(
        None, description="Only gaps that end after this instant"
    ),
    range_to: datetime | None = Query(
        None, description="Only gaps that start before this instant"
    ),
) -> dict[str, Any]:
    gaps = list_gaps(source=source, range_from=range_from, range_to=range_to)
    if gaps:
        log(
            logger,
            logging.CRITICAL,
            "DISCOVERY GAP DETECTED",
            source=source or "all",
            status="discovery_gap",
            record_count=len(gaps),
        )
    return {
        "gaps": gaps,
        "count": len(gaps),
        "note": (
            "Gaps are reported, not backfilled. Schedule catch-up from LiSN "
            "if the missing range must be collected."
        ),
    }


@api.get("/v1/health/detail")
def health_detail() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source, status, count(*)::int
                FROM collector_job
                GROUP BY source, status
                ORDER BY source, status
                """
            )
            by_source_status: dict[str, dict[str, int]] = {}
            for source, status, count in cur.fetchall():
                by_source_status.setdefault(source, {})[status] = count

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE status = 'in_progress'
                  AND lease_expires_at < now()
                """
            )
            stuck = cur.fetchone()[0]

            # procrastinate_jobs.worker_id is
            # REFERENCES procrastinate_workers(id) ON DELETE SET NULL, so after
            # pruning, any job left behind shows as doing with a null worker_id.
            # This should be zero in steady state — it is an ALERT condition,
            # not the primary recovery mechanism, because the sweeper's
            # get_stalled_jobs/retry_job should have caught them first.
            cur.execute(
                """
                SELECT count(*)::int
                FROM procrastinate_jobs
                WHERE status = 'doing'
                  AND worker_id IS NULL
                """
            )
            orphans = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*)::int
                FROM procrastinate_workers
                WHERE now() - last_heartbeat < interval '60 seconds'
                """
            )
            live_workers = cur.fetchone()[0]

            # Deployed worker count / identity visible over HTTP without shell.
            cur.execute(
                """
                SELECT id,
                       EXTRACT(EPOCH FROM (now() - last_heartbeat))::float
                         AS heartbeat_age_seconds
                FROM procrastinate_workers
                ORDER BY id
                """
            )
            workers = [
                {"id": wid, "heartbeat_age_seconds": age}
                for wid, age in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                  AND raw_written_at < now() - interval '15 minutes'
                """
            )
            unloaded = cur.fetchone()[0]

            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE status = 'dead'
                """
            )
            dead = cur.fetchone()[0]

            # Shortfall pages: done pages where fewer distinct entities came
            # back than keys asked. A shortfall is an ANOMALY worth surfacing,
            # not a failure worth alerting on — keys can legitimately not exist.
            cur.execute(
                """
                SELECT count(*)::int
                FROM collector_job
                WHERE status = 'done'
                  AND requested_count IS NOT NULL
                  AND returned_count IS NOT NULL
                  AND returned_count < requested_count
                """
            )
            shortfall_pages = cur.fetchone()[0]

    # discovery_gaps: the surface that previously showed all zeros while
    # skipped windows lost incidents. This is the specific thing the gap
    # ledger exists to change. Gaps are anomalies to surface — not auto-healed.
    discovery = gap_summary()
    partial = partial_windows_summary()

    return {
        "counts": by_source_status,
        "stuck": stuck,
        "orphans": orphans,
        "live_workers": live_workers,
        "workers": workers,
        "unloaded": unloaded,
        "dead": dead,
        "shortfall_pages": shortfall_pages,
        "discovery_gaps": discovery,
        "partial_windows": partial,
    }


# ---------------------------------------------------------------------------
# DELETE /v1/admin/collector-data — DESTRUCTIVE (primary)
# POST   /v1/admin/reset            — deprecated alias; same handler
#
# HARD DENYLIST — never clear, truncate, drop, or open for write:
#
#   sentinel_mock database — sentinel_incident and sentinel_thread.
#     This is the SAMPLE DATA the collector reads from. It must survive
#     untouched. When SENTINEL_MOCK_DSN is available the response reports
#     live counts and warns only if those counts change across the reset.
#     Never open sentinel_mock for write from this endpoint.
#
#   procrastinate_workers — NEVER truncate. procrastinate_jobs.worker_id
#     references it ON DELETE SET NULL. We already killed a live worker this
#     way: it registered as id 4, we truncated the table, its next fetch_job
#     tried to write worker_id=4 and hit
#     "procrastinate_jobs_worker_id_fkey ... Key (worker_id)=(4) is not present"
#     and the container exited(1). An HTTP endpoint cannot stop Cloud Run jobs,
#     so this path must be safe to call while workers are running — delete only
#     Procrastinate jobs that are not status='doing'.
#
#   Any table, bucket, dataset or resource belonging to Clariverse or anything
#     else in this project outside the HARD ALLOWLIST
#     (ADMIN_RESET_CLOUD_SQL_ALLOWLIST / ADMIN_RESET_BQ_ALLOWLIST / raw/ prefix).
# ---------------------------------------------------------------------------


def _admin_clear_collector_data(
    *,
    confirm: str,
    dry_run: bool,
    force: bool,
    request: Request,
) -> dict[str, Any]:
    """Shared body for DELETE collector-data and deprecated POST reset."""
    # Kill switch: turn off for a real pilot without a code change.
    if not admin_reset_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "admin collector-data clear is disabled in this environment "
                "(set ALLOW_ADMIN_RESET=1 to enable)"
            ),
        )

    if confirm != RESET_CONFIRM_PHRASE:
        raise HTTPException(
            status_code=400,
            detail=(
                f'confirm must be exactly "{RESET_CONFIRM_PHRASE}" '
                "(nothing was touched)"
            ),
        )

    caller = _caller_identity(request)
    try:
        return run_reset(dry_run=dry_run, force=force, caller=caller)
    except ResetInProgressError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "collector_job has in_progress rows; "
                    "pass force=true to reset anyway"
                ),
                "in_progress": exc.count,
                "job_ids": exc.job_ids,
            },
        ) from exc


@api.get(
    "/v1/admin/state",
    summary="Live counts of collector stores (read-only)",
    description=(
        "Returns current counts for every store DELETE /v1/admin/collector-data "
        "touches, plus expected sample-data sizes and procrastinate_workers. "
        "Use after a reset to verify clearance without four manual checks. "
        "Read-only; does not require ALLOW_ADMIN_RESET."
    ),
    tags=["admin"],
)
def admin_state() -> dict[str, Any]:
    warnings: list[str] = []
    return collect_live_state(warnings)


@api.get(
    "/v1/admin/sample-ids",
    summary="Sample incident IDs from BigQuery landing table",
    description=(
        "Returns distinct incident ids from sentinel_raw.incidents_v2 for the "
        "demo UI 'Load sample IDs' button. Same Cloud Run auth as the rest of "
        "the API — not a public shortcut."
    ),
    tags=["admin"],
)
def admin_sample_ids(
    source: str = Query("sentinel", description="Landing source (sentinel only)"),
    limit: int = Query(1000, ge=1, le=5000),
) -> dict[str, Any]:
    if source != "sentinel":
        raise HTTPException(
            status_code=400,
            detail=f"sample-ids supports source=sentinel only, got {source!r}",
        )
    project = os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise HTTPException(status_code=500, detail="PROJECT unset")
    region = os.environ.get("REGION", "asia-south1")
    table = f"`{project}.sentinel_raw.incidents_v2`"
    client = bigquery.Client(project=project, location=region)
    try:
        rows = list(
            client.query(
                f"""
                SELECT DISTINCT id
                FROM {table}
                WHERE id IS NOT NULL
                ORDER BY id
                LIMIT @limit
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("limit", "INT64", limit),
                    ]
                ),
                location=region,
            ).result()
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"BigQuery sample-ids failed: {redact_secrets(str(exc))}",
        ) from exc

    ids = [str(row.id) for row in rows if row.id is not None]
    if not ids:
        return {
            "source": source,
            "key_type": "incident_ids",
            "ids": [],
            "count": 0,
            "message": (
                "sentinel_raw.incidents_v2 is empty — run a collection or use "
                "the date mode first"
            ),
        }
    return {
        "source": source,
        "key_type": "incident_ids",
        "ids": ids,
        "count": len(ids),
    }


@api.delete(
    "/v1/admin/collector-data",
    summary="Delete collector output data (preferred)",
    description=(
        "Clears collector *output* only (Cloud SQL allowlist, GCS raw/, BQ "
        "landing tables). Safe with workers running — never truncates "
        "procrastinate_workers; never opens sentinel_mock.\n\n"
        "Query parameters: confirm (required), dry_run (default true), force "
        "(default false).\n\n"
        "Trade-off: query parameters appear in Cloud Run request logs, so the "
        "confirm token is logged. The deprecated POST body variant does not "
        "have this property. This is acceptable because the token is a typo "
        "guard, not a secret — the real protection is Cloud Run authentication "
        "plus ALLOW_ADMIN_RESET.\n\n"
        "Response includes success=true only when warnings is empty; after "
        "counts are always re-read from the real store."
    ),
    tags=["admin"],
)
def delete_collector_data(
    request: Request,
    confirm: str = Query(
        ...,
        description='Must be exactly "reset-collector-data"',
    ),
    dry_run: bool = Query(
        True,
        description="Default true — pass false to actually delete",
    ),
    force: bool = Query(
        False,
        description="Override in_progress refusal",
    ),
) -> dict[str, Any]:
    # confirm is a query param → visible in Cloud Run request logs. That is a
    # typo guard, not a secret; auth + ALLOW_ADMIN_RESET are the real controls.
    return _admin_clear_collector_data(
        confirm=confirm,
        dry_run=dry_run,
        force=force,
        request=request,
    )


@api.post(
    "/v1/admin/reset",
    summary="[Deprecated] Alias for DELETE /v1/admin/collector-data",
    description=(
        "DEPRECATED — use DELETE /v1/admin/collector-data instead. Kept as an "
        "alias so existing scripts and tests keep working. Same allowlist, "
        "guards, and response as the DELETE route; parameters are in the JSON "
        "body (confirm, dry_run, force)."
    ),
    deprecated=True,
    tags=["admin"],
)
def admin_reset(body: AdminResetBody, request: Request) -> dict[str, Any]:
    return _admin_clear_collector_data(
        confirm=body.confirm,
        dry_run=body.dry_run,
        force=body.force,
        request=request,
    )
