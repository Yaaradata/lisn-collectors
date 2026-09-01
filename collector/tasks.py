"""Procrastinate task body: fetch one collector_job page end-to-end.

Also hosts the Sprint 4 sweeper that recovers stranded Procrastinate jobs
(Layer A) and expired collector_job leases (Layer B).

Tracing: one span per PAGE (and per discovery cursor page inside fetch), never
per record. At ~300k incidents, row-level spans would make SigNoz unusable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import procrastinate
from psycopg import errors as pg_errors

from collector.app import app
from collector.contract import Page
from collector.db import connect
from collector.discovery_gaps import (
    mark_window_failed,
    maybe_finalize_window,
    reconcile_running_windows,
)
from collector.load import append_records
from collector.logging_setup import get_logger, log
from collector.metrics import (
    record_jobs_dead_lettered,
    record_jobs_requeued,
    record_page_completed,
    record_page_shortfall,
    record_records_landed,
    timed_stage,
)
from collector.raw import write_raw
from collector.redact import redact_secrets
from collector.shortfall import requested_count, returned_count, shortfall_keys
from collector.sources import get
from collector.tracing import parent_context, task_index, traced_span

logger = get_logger(__name__)


@app.task(
    queue="sentinel",
    name="fetch_page",
    retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=4),
)
def fetch_page(job_id: str, worker_id: str = app.WORKER_ID) -> None:
    # Procrastinate carries a pointer, not a payload. The page payload lives in
    # our collector_job row, so our table stays the source of truth and
    # Procrastinate's schema stays an implementation detail.
    request_id: str | None = None
    source_name: str | None = None
    page_no: int | None = None
    attempt: int | None = None
    t0 = time.monotonic()

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text, request_id::text, source, page_no,
                       page_payload, status, coalesce(priority, 0),
                       trace_context, coalesce(requested_count, 0)
                FROM collector_job
                WHERE job_id = %s::uuid
                """,
                (job_id,),
            )
            row = cur.fetchone()
    if row is None:
        return
    (
        _jid,
        request_id,
        source_name,
        page_no,
        page_payload,
        status,
        job_priority,
        trace_ctx,
        req_count_stored,
    ) = row
    if status in ("done", "dead"):
        return

    idx = task_index()
    page_attrs = {
        "job_id": job_id,
        "page_no": page_no,
        "worker_id": worker_id,
        "source": source_name,
        "request_id": request_id,
        "requested_count": req_count_stored,
        "lisn.job_id": job_id,
        "lisn.page_no": page_no,
        "lisn.worker_id": worker_id,
        "lisn.source": source_name,
        "lisn.request_id": request_id,
        "lisn.requested_count": req_count_stored,
    }
    if idx is not None:
        page_attrs["task_index"] = idx
        page_attrs["lisn.task_index"] = idx

    # Continue the API collect_request trace — parent comes from Postgres, not HTTP.
    try:
        with traced_span(
            "fetch_page",
            parent=parent_context(trace_ctx),
            attributes=page_attrs,
        ) as page_span:
            # --- STEP 0 — Claim -------------------------------------------------
            with timed_stage(source=source_name, stage="claim_job"):
                with traced_span("claim_job"):
                    with connect() as conn:
                        with conn.cursor() as cur:
                            # Killswitch: if this source is paused, put the row back to
                            # pending and leave without calling the source. The job returns
                            # to the queue rather than failing, so nothing is lost and work
                            # resumes the moment the flag clears.
                            control = None
                            try:
                                cur.execute(
                                    """
                                    SELECT paused
                                    FROM collector_control
                                    WHERE source = %s
                                    """,
                                    (source_name,),
                                )
                                control = cur.fetchone()
                            except pg_errors.UndefinedTable:
                                # Table is created by scripts/11_killswitch.sh on first use.
                                conn.rollback()

                            if control is not None and control[0]:
                                cur.execute(
                                    """
                                    UPDATE collector_job
                                    SET status = 'pending',
                                        owner = NULL,
                                        lease_expires_at = NULL,
                                        updated_at = now()
                                    WHERE job_id = %s::uuid
                                    """,
                                    (job_id,),
                                )
                                conn.commit()
                                # Re-defer so a Procrastinate worker picks it up again after
                                # the flag clears (returning success would finish the job).
                                # Preserve priority — otherwise a paused urgent page drops
                                # to the back of the queue when the flag clears.
                                fetch_page.configure(
                                    queue=source_name,
                                    schedule_in={"seconds": 15},
                                    priority=int(job_priority),
                                ).defer(job_id=job_id)
                                page_span.set_attribute("lisn.paused", True)
                                return

                            src = get(source_name)
                            lease_until = datetime.now(timezone.utc) + timedelta(
                                seconds=src.lease_seconds
                            )
                            cur.execute(
                                """
                                UPDATE collector_job
                                SET status = 'in_progress',
                                    owner = %s,
                                    attempts = attempts + 1,
                                    lease_expires_at = %s,
                                    updated_at = now()
                                WHERE job_id = %s::uuid
                                RETURNING attempts
                                """,
                                (worker_id, lease_until, job_id),
                            )
                            attempt = cur.fetchone()[0]
                        conn.commit()

            page_span.set_attribute("attempt", attempt)
            page_span.set_attribute("lisn.attempt", attempt)
            log(
                logger,
                logging.INFO,
                "page claimed by worker",
                request_id=request_id,
                job_id=job_id,
                source=source_name,
                page_no=page_no,
                worker_id=worker_id,
                attempt=attempt,
                status="in_progress",
            )
            if attempt is not None and attempt > 1:
                log(
                    logger,
                    logging.WARNING,
                    "page retry",
                    request_id=request_id,
                    job_id=job_id,
                    source=source_name,
                    page_no=page_no,
                    worker_id=worker_id,
                    attempt=attempt,
                    status="retry",
                )

            page = Page(page_no=page_no, payload=page_payload)

            # --- STEP 1 — Rate --------------------------------------------------
            # This is the ONLY rate control. Procrastinate has no rate limiting.
            # With N worker instances the ceiling is N x (1/min_interval_s). A
            # rolling deploy briefly runs old and new instances together and doubles it.
            with timed_stage(source=source_name, stage="rate_wait"):
                with traced_span(
                    "rate_wait",
                    attributes={
                        "lisn.min_interval_s": src.min_interval_s,
                    },
                ):
                    log(
                        logger,
                        logging.DEBUG,
                        "rate sleep",
                        request_id=request_id,
                        job_id=job_id,
                        source=source_name,
                        page_no=page_no,
                        worker_id=worker_id,
                        attempt=attempt,
                        duration_ms=int(src.min_interval_s * 1000),
                    )
                    time.sleep(src.min_interval_s)

            # --- STEP 2 — Fetch (source_fetch span emitted inside src.fetch) ---
            with timed_stage(source=source_name, stage="source_fetch"):
                raw = src.fetch(page)
            log(
                logger,
                logging.DEBUG,
                "source fetch response bytes",
                request_id=request_id,
                job_id=job_id,
                source=source_name,
                page_no=page_no,
                worker_id=worker_id,
                attempt=attempt,
                record_count=(
                    len(raw.body)
                    if isinstance(raw.body, (bytes, bytearray, str))
                    else None
                ),
            )

            # --- STEP 3 — Raw landing -------------------------------------------
            bucket_name = os.environ.get("RAW_BUCKET", "")
            with timed_stage(source=source_name, stage="write_raw_gcs"):
                with traced_span(
                    "write_raw_gcs",
                    attributes={"gcs.bucket": bucket_name},
                ) as gcs_span:
                    uri, size, digest = write_raw(
                        source_name,
                        request_id,
                        page_no,
                        raw.body,
                        raw.content_type,
                    )
                    gcs_span.set_attribute("gcs.object_size", size)
                    gcs_span.set_attribute("gcs.sha256_prefix", digest[:16])
                    with connect() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO raw_manifest (
                                  raw_uri, job_id, request_id, source, page_no,
                                  record_count, byte_size, sha256
                                )
                                VALUES (
                                  %s, %s::uuid, %s::uuid, %s, %s,
                                  0, %s, %s
                                )
                                ON CONFLICT (raw_uri) DO UPDATE SET
                                  job_id = EXCLUDED.job_id,
                                  request_id = EXCLUDED.request_id,
                                  source = EXCLUDED.source,
                                  page_no = EXCLUDED.page_no,
                                  record_count = EXCLUDED.record_count,
                                  byte_size = EXCLUDED.byte_size,
                                  sha256 = EXCLUDED.sha256,
                                  written_at = now()
                                """,
                                (
                                    uri,
                                    job_id,
                                    request_id,
                                    source_name,
                                    page_no,
                                    size,
                                    digest,
                                ),
                            )
                            cur.execute(
                                """
                                UPDATE collector_job
                                SET raw_uri = %s,
                                    raw_written_at = now(),
                                    updated_at = now()
                                WHERE job_id = %s::uuid
                                """,
                                (uri, job_id),
                            )
                        conn.commit()
            # One page body landed in GCS — count as one "record" unit for the
            # destination counter (byte size is on the span/attrs separately).
            record_records_landed(
                source=source_name, destination="gcs", count=1
            )

            # --- STEP 4 — Parse and load ----------------------------------------
            # One span for the whole parse — never one span per Record.
            with timed_stage(source=source_name, stage="parse_records"):
                with traced_span("parse_records") as parse_span:
                    records = src.parse(raw, page)
                    entities_returned = returned_count(records, page.payload)
                    req_n = requested_count(page.payload)
                    parse_span.set_attribute("lisn.records_parsed", len(records))
                    parse_span.set_attribute(
                        "lisn.distinct_incidents", entities_returned
                    )
                    if entities_returned > 0:
                        parse_span.set_attribute(
                            "lisn.explosion_factor",
                            round(len(records) / entities_returned, 3),
                        )

            with timed_stage(source=source_name, stage="load_bigquery"):
                with traced_span(
                    "load_bigquery",
                    attributes={
                        "lisn.bq_table": src.bq_table,
                        "lisn.bq_insert_mode": "streaming",
                    },
                ) as bq_span:
                    n = append_records(
                        src.bq_table, records, request_id, page_no, uri
                    )
                    bq_span.set_attribute("lisn.rows_inserted", n)
            record_records_landed(
                source=source_name, destination="bigquery", count=n
            )

            keys_delta = shortfall_keys(page, records)
            if keys_delta:
                record_page_shortfall(source=source_name)
                log(
                    logger,
                    logging.WARNING,
                    "shortfall — fewer records returned than keys requested",
                    request_id=request_id,
                    job_id=job_id,
                    source=source_name,
                    page_no=page_no,
                    worker_id=worker_id,
                    attempt=attempt,
                    status="shortfall",
                    requested_count=req_n,
                    returned_count=entities_returned,
                    record_count=n,
                )

            # --- STEP 5 — Complete, LAST ----------------------------------------
            # This UPDATE runs AFTER the BigQuery write commits, never after the
            # fetch. If the worker dies between steps 3 and 4, the row stays
            # in_progress, the Sprint 4 sweeper requeues it, and both writes are
            # redone safely — the GCS object overwrites itself and BigQuery rows
            # deduplicate in the sentinel_core view. Marking done before the load
            # would make that failure silent.
            duration_ms = int((time.monotonic() - t0) * 1000)
            with timed_stage(source=source_name, stage="mark_complete"):
                with traced_span(
                    "mark_complete",
                    attributes={"lisn.page_duration_ms": duration_ms},
                ):
                    with connect() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE raw_manifest
                                SET record_count = %s
                                WHERE raw_uri = %s
                                """,
                                (n, uri),
                            )
                            cur.execute(
                                """
                                UPDATE collector_job
                                SET status = 'done',
                                    loaded_at = now(),
                                    record_count = %s,
                                    returned_count = %s,
                                    missing_keys = %s::jsonb,
                                    lease_expires_at = NULL,
                                    last_error = NULL,
                                    updated_at = now()
                                WHERE job_id = %s::uuid
                                """,
                                (
                                    n,
                                    entities_returned,
                                    json.dumps(keys_delta)
                                    if keys_delta is not None
                                    else None,
                                    job_id,
                                ),
                            )
                        conn.commit()
            record_page_completed(source=source_name, status="done")

            log(
                logger,
                logging.INFO,
                "page completed",
                request_id=request_id,
                job_id=job_id,
                source=source_name,
                page_no=page_no,
                worker_id=worker_id,
                attempt=attempt,
                status="done",
                duration_ms=duration_ms,
                record_count=n,
                requested_count=req_n,
                returned_count=entities_returned,
            )
            if source_name == "sentinel_discovery":
                # (a) Immediate finalise when all pages for this request are terminal.
                maybe_finalize_window(request_id)
    except Exception as exc:
        err = redact_secrets(str(exc)) or ""
        attempts_now: int | None = attempt
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE collector_job
                    SET last_error = %s,
                        updated_at = now()
                    WHERE job_id = %s::uuid
                    RETURNING attempts
                    """,
                    (err[:4000], job_id),
                )
                row = cur.fetchone()
                if row is not None:
                    attempts_now = row[0]
            conn.commit()
        log(
            logger,
            logging.ERROR,
            f"page failed; will retry: {err[:500]}",
            request_id=request_id,
            job_id=job_id,
            source=source_name,
            page_no=page_no,
            worker_id=worker_id,
            attempt=attempts_now,
            status="error",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        if source_name:
            record_page_completed(source=source_name, status="failed")
        # Procrastinate max_attempts=3; collector_job dead-letters at attempts>=5
        # via the sweeper. Surface exhaustion before that as ERROR.
        if attempts_now is not None and attempts_now >= 3:
            log(
                logger,
                logging.ERROR,
                "page exhausted its attempts",
                request_id=request_id,
                job_id=job_id,
                source=source_name,
                page_no=page_no,
                worker_id=worker_id,
                attempt=attempts_now,
                status="exhausted",
            )
        # Keep exception text out of the raise path's default logging noise;
        # last_error already holds the redacted form. Span ERROR status is set
        # by traced_span on fetch_page.
        raise


async def _run_sweep() -> dict[str, int]:
    """Recover stranded Procrastinate jobs (A) and expired collector_job leases (B)."""
    with traced_span("sweep") as sweep_span:
        # --- LAYER A — Procrastinate stalls ---------------------------------
        # get BEFORE prune. get_stalled_jobs identifies jobs by joining to worker
        # heartbeats, so pruning first deletes the worker rows and makes those jobs
        # invisible to the query — leaving them at status='doing' with worker_id IS NULL
        # and no heartbeat-based way to find them.
        stalled = list(
            await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=60)
        )
        # retry_job moves the job from doing back to todo so any healthy worker
        # picks it up. It does not create a new job and it preserves attempts.
        for job in stalled:
            await app.job_manager.retry_job(job)
        pruned = await app.job_manager.prune_stalled_workers(
            seconds_since_heartbeat=60
        )

        # --- LAYER B — our collector_job leases -----------------------------
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE collector_job
                    SET status = 'pending',
                        owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = now()
                    WHERE status = 'in_progress'
                      AND lease_expires_at < now()
                      AND attempts < 5
                    RETURNING job_id::text, source, coalesce(priority, 0),
                              request_id::text, page_no, attempts
                    """
                )
                requeued = cur.fetchall()

                cur.execute(
                    """
                    UPDATE collector_job
                    SET status = 'dead',
                        updated_at = now()
                    WHERE status = 'in_progress'
                      AND lease_expires_at < now()
                      AND attempts >= 5
                    RETURNING request_id::text, source, job_id::text, page_no, attempts
                    """
                )
                dead_rows = cur.fetchall()
                dead_lettered = len(dead_rows)
                failed_discovery_requests = {
                    rid
                    for rid, src, *_rest in dead_rows
                    if src == "sentinel_discovery"
                }

                # THE DOUBLE-RECOVERY TRAP: if Layer A already retried the job, it
                # will re-run fetch_page(job_id) on its own. Deferring another would
                # put two workers on the same page — one wasted call against our
                # Sentinel rate ceiling. The task body is idempotent so nothing
                # corrupts, but a wasted call is still a wasted call.
                to_defer: list[tuple[str, str, int]] = []
                redefers_skipped = 0
                for (
                    job_id,
                    source,
                    priority,
                    request_id,
                    page_no,
                    attempts,
                ) in requeued:
                    cur.execute(
                        """
                        SELECT 1
                        FROM procrastinate_jobs
                        WHERE args->>'job_id' = %s
                          AND status IN ('todo', 'doing')
                        LIMIT 1
                        """,
                        (job_id,),
                    )
                    if cur.fetchone() is not None:
                        redefers_skipped += 1
                    else:
                        to_defer.append((job_id, source, int(priority)))
                    record_jobs_requeued(source=source, count=1)
                    log(
                        logger,
                        logging.WARNING,
                        "sweeper requeued stranded job",
                        request_id=request_id,
                        job_id=job_id,
                        source=source,
                        page_no=page_no,
                        attempt=attempts,
                        status="requeued",
                    )
            conn.commit()

        for rid, src, job_id, page_no, attempts in dead_rows:
            record_jobs_dead_lettered(source=src, count=1)
            record_page_completed(source=src, status="dead")
            log(
                logger,
                logging.CRITICAL,
                "page dead-lettered",
                request_id=rid,
                job_id=job_id,
                source=src,
                page_no=page_no,
                attempt=attempts,
                status="dead",
            )

        for rid in failed_discovery_requests:
            mark_window_failed(rid)

        # (b) Backstop: any running discovery_window whose pages are already all
        # terminal (done/dead) gets finalised here — covers the crash window
        # between page-done commit and maybe_finalize_window in fetch_page.
        windows_finalised = reconcile_running_windows()

        for job_id, source, priority in to_defer:
            # Preserve priority on requeue — without this an urgent page recovered
            # by the sweeper silently drops to the back of the queue (priority 0).
            await fetch_page.configure(
                queue=source, priority=priority
            ).defer_async(job_id=job_id)

        result = {
            "stalled_jobs_retried": len(stalled),
            "workers_pruned": len(pruned),
            "rows_requeued": len(requeued),
            "rows_dead_lettered": dead_lettered,
            "redefers_skipped": redefers_skipped,
            "discovery_windows_finalised": windows_finalised,
        }
        for key, value in result.items():
            sweep_span.set_attribute(key, value)
            sweep_span.set_attribute(f"lisn.{key}", value)

        if any(result.values()):
            log(
                logger,
                logging.INFO,
                "sweep result",
                status="sweep",
                record_count=result["rows_requeued"],
                returned_count=result["rows_dead_lettered"],
                requested_count=result["stalled_jobs_retried"],
            )
            # Keep the numeric breakdown in the message for operators; attributes
            # above give SigNoz filterable counts for the main outcomes.
            logger.info(
                "sweep stalled=%s pruned=%s requeued=%s dead=%s skip_redefer=%s "
                "windows_finalised=%s",
                result["stalled_jobs_retried"],
                result["workers_pruned"],
                result["rows_requeued"],
                result["rows_dead_lettered"],
                result["redefers_skipped"],
                result["discovery_windows_finalised"],
            )
        return result


@app.periodic(cron="*/2 * * * *")
@app.task(queue="maintenance", name="sweep")
async def sweep(timestamp: int) -> dict[str, int]:
    # timestamp is REQUIRED by @app.periodic (cron tick epoch seconds).
    _ = timestamp
    return await _run_sweep()


# Waiting two minutes for the cron tick in front of an audience is bad demo pacing.
@app.task(queue="maintenance", name="sweep_now")
async def sweep_now() -> dict[str, int]:
    """Manual trigger for demos — same body as the periodic sweep."""
    return await _run_sweep()
