"""Section D tests (idempotency, determinism, merge correctness)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import psycopg
import pytest

from collector.raw import write_raw
from tests.audit.fakes import FakeBQ, FakeGCS, SinkPatch
from tests.audit.helpers import (
    fetch_incident_ids,
    reset_state,
    resolved_table_id,
    run_jobs_with_fakes,
    seed_jobs_for_incident_ids,
    write_evidence,
)


def test_d01_raw_path_across_utc_midnight(require_fakes_selftest: None) -> None:
    test_id = "D-01"
    import collector.raw as raw_mod

    class FakeDateTime:
        values = [
            datetime(2026, 8, 25, 23, 59, 59, tzinfo=timezone.utc),
            datetime(2026, 8, 26, 0, 0, 1, tzinfo=timezone.utc),
        ]

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            del tz
            return cls.values.pop(0)

    original = raw_mod.datetime
    try:
        raw_mod.datetime = FakeDateTime
        with SinkPatch():
            uri1, _, _ = write_raw("sentinel", "req-midnight", 0, b'{"a":1}', "application/json")
            uri2, _, _ = write_raw("sentinel", "req-midnight", 0, b'{"a":1}', "application/json")
    finally:
        raw_mod.datetime = original

    write_evidence(
        test_id,
        [
            f"uri1={uri1}",
            f"uri2={uri2}",
            "fully_measured=yes (path derivation occurs in collector/raw.py before any sink interaction)",
        ],
    )
    assert uri1 == uri2, "path changes across UTC midnight"


def test_d02_same_day_rewrite(require_fakes_selftest: None) -> None:
    test_id = "D-02"
    reset_state()
    with SinkPatch():
        uri1, _, sha1 = write_raw("sentinel", "req-sameday", 0, b'{"b":2}', "application/json")
        uri2, _, sha2 = write_raw("sentinel", "req-sameday", 0, b'{"b":2}', "application/json")
    objects = FakeGCS.list_objects("audit-bucket")
    write_evidence(
        test_id,
        [
            f"uri1={uri1}",
            f"uri2={uri2}",
            f"sha1={sha1}",
            f"sha2={sha2}",
            f"object_count={len(objects)}",
        ],
    )
    assert uri1 == uri2
    assert sha1 == sha2
    assert len(objects) == 1


def test_d03_full_replay(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "D-03"
    reset_state()
    table_id = resolved_table_id()
    ids = fetch_incident_ids(200)
    request_id, job_ids = seed_jobs_for_incident_ids(ids)
    run_jobs_with_fakes(job_ids)
    rows_first = len(FakeBQ.Client.fetch_rows(table_id))
    objects_first = len(FakeGCS.list_objects("audit-bucket"))

    from collector.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_job
                SET status='pending', owner=NULL, lease_expires_at=NULL
                WHERE request_id=%s::uuid
                """,
                (request_id,),
            )
        conn.commit()

    run_jobs_with_fakes(job_ids)
    rows_second = len(FakeBQ.Client.fetch_rows(table_id))
    objects_second = len(FakeGCS.list_objects("audit-bucket"))
    write_evidence(
        test_id,
        [
            f"request_id={request_id}",
            f"rows_first={rows_first}",
            f"rows_second={rows_second}",
            f"objects_first={objects_first}",
            f"objects_second={objects_second}",
        ],
    )
    assert rows_second == rows_first * 2
    assert objects_second == objects_first


def test_d04_missing_thread_id(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "D-04"
    write_evidence(
        test_id,
        [
            "BLOCKED: requires incidents_current merge semantics (BigQuery view), not representable in FakeBQ append store.",
            "missing_precondition=bigquery_view_execution_for_sentinel_core.incidents_current",
        ],
    )
    pytest.skip("BLOCKED: missing BigQuery view semantics precondition")


def test_d05_ingested_at_under_streaming_insert(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "D-05"
    reset_state()
    table_id = resolved_table_id()
    ids = fetch_incident_ids(50)
    _, job_ids = seed_jobs_for_incident_ids(ids)
    run_jobs_with_fakes(job_ids)
    rows = FakeBQ.Client.fetch_rows(table_id)
    null_or_missing = sum(
        1 for row in rows if ("_ingested_at" not in row or row.get("_ingested_at") is None)
    )
    write_evidence(
        test_id,
        [
            f"rows_total={len(rows)}",
            f"rows_null_or_missing__ingested_at={null_or_missing}",
            "BLOCKED: FakeBQ intentionally does not auto-populate _ingested_at; this does not measure real BigQuery streaming default behavior.",
            "clariverse_query=SELECT count(*) AS null_ingested_at FROM `PROJECT.sentinel_raw.incidents` WHERE _ingested_at IS NULL;",
            "owner=Ranjith BK",
            "missing_precondition=real_bigquery_streaming_insert_execution",
        ],
    )
    pytest.skip("BLOCKED: requires real BigQuery streaming insert behavior on clariversev1")


def test_d06_merge_key_is_genuinely_composite(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "D-06"
    write_evidence(
        test_id,
        [
            "BLOCKED: requires BigQuery view ROW_NUMBER PARTITION BY (id, threads_id) behavior.",
            "missing_precondition=bigquery_view_execution_for_merge_semantics",
        ],
    )
    pytest.skip("BLOCKED: missing BigQuery view semantics precondition")


def test_d07_thread_explosion_factor_end_to_end(require_fakes_selftest: None, require_pipeline_smoke: None) -> None:
    test_id = "D-07"
    reset_state()
    table_id = resolved_table_id()
    ids = fetch_incident_ids(1000)
    request_id, job_ids = seed_jobs_for_incident_ids(ids)
    run_jobs_with_fakes(job_ids)

    dsn = os.environ["COLLECTOR_DSN"]
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) AS total_rows,
                       count(DISTINCT row->>'id') AS distinct_ids
                FROM audit_bq_rows
                WHERE table_id = %s
                """,
                (table_id,),
            )
            total_rows, distinct_ids = cur.fetchone()
    factor = (total_rows / distinct_ids) if distinct_ids else 0.0
    write_evidence(
        test_id,
        [
            f"request_id={request_id}",
            f"table_id={table_id}",
            f"total_rows={total_rows}",
            f"distinct_ids={distinct_ids}",
            f"factor={factor:.3f}",
        ],
    )
    assert distinct_ids == 1000
    assert abs(factor - 2.481) <= (2.481 * 0.02)
