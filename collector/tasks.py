"""Procrastinate task body: fetch one collector_job page end-to-end.

Also hosts the Sprint 4 sweeper that recovers stranded Procrastinate jobs
(Layer A) and expired collector_job leases (Layer B).
"""

from __future__ import annotations

import json
import logging
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
from collector.raw import write_raw
from collector.redact import redact_secrets
from collector.shortfall import returned_count, shortfall_keys
from collector.sources import get

logger = logging.getLogger(__name__)


@app.task(
    queue="sentinel",
    name="fetch_page",
    retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=4),
)
def fetch_page(job_id: str, worker_id: str = app.WORKER_ID) -> None:
    # Procrastinate carries a pointer, not a payload. The page payload lives in
    # our collector_job row, so our table stays the source of truth and
    # Procrastinate's schema stays an implementation detail.
    try:
        # --- STEP 0 — Claim -------------------------------------------------
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id::text, request_id::text, source, page_no,
                           page_payload, status, coalesce(priority, 0)
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
                ) = row
                if status in ("done", "dead"):
                    return

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
                    """,
                    (worker_id, lease_until, job_id),
                )
            conn.commit()

        page = Page(page_no=page_no, payload=page_payload)

        # --- STEP 1 — Rate --------------------------------------------------
        # This is the ONLY rate control. Procrastinate has no rate limiting.
        # With N worker instances the ceiling is N x (1/min_interval_s). A
        # rolling deploy briefly runs old and new instances together and doubles it.
        time.sleep(src.min_interval_s)

        # --- STEP 2 — Fetch -------------------------------------------------
        raw = src.fetch(page)

        # --- STEP 3 — Raw landing -------------------------------------------
        uri, size, digest = write_raw(
            source_name,
            request_id,
            page_no,
            raw.body,
            raw.content_type,
        )
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
                    (uri, job_id, request_id, source_name, page_no, size, digest),
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

        # --- STEP 4 — Parse and load ----------------------------------------
        records = src.parse(raw, page)
        n = append_records(src.bq_table, records, request_id, page_no, uri)
        # Distinct source entities (not thread rows). See collector_job column
        # comments: requested_count / returned_count / record_count.
        entities_returned = returned_count(records, page.payload)
        keys_delta = shortfall_keys(page, records)

        # --- STEP 5 — Complete, LAST ----------------------------------------
        # This UPDATE runs AFTER the BigQuery write commits, never after the
        # fetch. If the worker dies between steps 3 and 4, the row stays
        # in_progress, the Sprint 4 sweeper requeues it, and both writes are
        # redone safely — the GCS object overwrites itself and BigQuery rows
        # deduplicate in the sentinel_core view. Marking done before the load
        # would make that failure silent.
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
                        json.dumps(keys_delta) if keys_delta is not None else None,
                        job_id,
                    ),
                )
            conn.commit()
        if source_name == "sentinel_discovery":
            # (a) Immediate finalise when all pages for this request are terminal.
            maybe_finalize_window(request_id)
    except Exception as exc:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE collector_job
                    SET last_error = %s,
                        updated_at = now()
                    WHERE job_id = %s::uuid
                    """,
                    (redact_secrets(str(exc))[:4000], job_id),
                )
            conn.commit()
        raise


async def _run_sweep() -> dict[str, int]:
    """Recover stranded Procrastinate jobs (A) and expired collector_job leases (B)."""
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
    pruned = await app.job_manager.prune_stalled_workers(seconds_since_heartbeat=60)

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
                RETURNING job_id::text, source, coalesce(priority, 0)
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
                RETURNING request_id::text, source
                """
            )
            dead_rows = cur.fetchall()
            dead_lettered = len(dead_rows)
            failed_discovery_requests = {
                rid for rid, src in dead_rows if src == "sentinel_discovery"
            }

            # THE DOUBLE-RECOVERY TRAP: if Layer A already retried the job, it
            # will re-run fetch_page(job_id) on its own. Deferring another would
            # put two workers on the same page — one wasted call against our
            # Sentinel rate ceiling. The task body is idempotent so nothing
            # corrupts, but a wasted call is still a wasted call.
            to_defer: list[tuple[str, str, int]] = []
            redefers_skipped = 0
            for job_id, source, priority in requeued:
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
        conn.commit()

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
    if any(result.values()):
        logger.info("sweep %s", result)
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
