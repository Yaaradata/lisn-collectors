"""Procrastinate task body: fetch one collector_job page end-to-end."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import procrastinate

from collector.app import app
from collector.contract import Page
from collector.db import connect
from collector.load import append_records
from collector.raw import write_raw
from collector.sources import get


@app.task(
    queue="sentinel",
    name="fetch_page",
    retry=procrastinate.RetryStrategy(max_attempts=3, exponential_wait=4),
)
def fetch_page(job_id: str, worker_id: str = "w") -> None:
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
                           page_payload, status
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
                ) = row
                if status in ("done", "dead"):
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
                        lease_expires_at = NULL,
                        last_error = NULL,
                        updated_at = now()
                    WHERE job_id = %s::uuid
                    """,
                    (n, job_id),
                )
            conn.commit()
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
                    (str(exc)[:4000], job_id),
                )
            conn.commit()
        raise
