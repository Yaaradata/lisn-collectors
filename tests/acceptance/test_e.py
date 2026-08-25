from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

import psycopg

from collector.app import app as procrastinate_app
from collector.tasks import sweep
from tests.acceptance.helpers import (
    dataset_by_code,
    incident_ids_from_identity_set,
    post_collect,
    reset_collector_state,
    write_evidence,
)

UTC = timezone.utc


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _api_get_json(path: str) -> dict[str, Any]:
    req = urllib.request.Request(f"http://127.0.0.1:8080{path}", method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_sweep_now() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with procrastinate_app.open_async():
            return await sweep(int(datetime.now(tz=UTC).timestamp()))

    return asyncio.run(_run())


def _single_page_request() -> str:
    ds1 = dataset_by_code("DS-1")
    ds1.build()
    incident_ids = incident_ids_from_identity_set(
        {
            (
                str(row["id"]),
                None if row.get("thread_id") is None else str(row.get("thread_id")),
            )
            for row in ds1.truth()
        }
    )
    return post_collect("sentinel", {"incident_ids": incident_ids[:50]})


def _job_id_for_request(request_id: str) -> str:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text
                FROM collector_job
                WHERE request_id = %s::uuid
                ORDER BY page_no
                LIMIT 1
                """,
                (request_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise AssertionError(f"no collector_job for request {request_id}")
    return str(row[0])


def _set_job_state(
    job_id: str,
    *,
    status: str,
    attempts: int,
    lease_seconds_offset: int,
    last_error: str,
) -> None:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_job
                SET status = %s,
                    attempts = %s,
                    lease_expires_at = now() + make_interval(secs => %s),
                    last_error = %s,
                    updated_at = now()
                WHERE job_id = %s::uuid
                """,
                (status, attempts, lease_seconds_offset, last_error, job_id),
            )
        conn.commit()


def _job_status(job_id: str) -> str:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM collector_job WHERE job_id = %s::uuid",
                (job_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise AssertionError(f"missing collector_job {job_id}")
    return str(row[0])


def test_e10_dead_letter_runs_to_conclusion_with_30m_cap() -> None:
    reset_collector_state()
    request_id = _single_page_request()
    job_id = _job_id_for_request(request_id)
    _set_job_state(
        job_id,
        status="in_progress",
        attempts=5,
        lease_seconds_offset=-120,
        last_error="E-10 forced dead-letter path",
    )

    start = time.monotonic()
    cap_s = 30 * 60
    time_to_dead_letter_s: float | None = None
    sweeps: list[dict[str, Any]] = []
    while True:
        sweeps.append(_run_sweep_now())
        dead = _api_get_json("/v1/dead-letter")
        rows = dead.get("rows", [])
        if any(str(r.get("job_id")) == job_id for r in rows):
            time_to_dead_letter_s = time.monotonic() - start
            break
        if time.monotonic() - start > cap_s:
            break
        time.sleep(2)

    elapsed_s = time.monotonic() - start
    print(f"E-10 recovery_latency_s=N/A")
    print(f"E-10 time_to_dead_letter_s={time_to_dead_letter_s}")
    print(f"E-10 elapsed_s={elapsed_s:.3f}")
    write_evidence(
        "E-10",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"time_to_dead_letter_s={time_to_dead_letter_s}",
            f"elapsed_s={elapsed_s:.6f}",
            f"final_job_status={_job_status(job_id)}",
            f"sweeps={json.dumps(sweeps, sort_keys=True)}",
        ],
    )
    assert elapsed_s <= cap_s, f"E-10 exceeded cap: {elapsed_s:.3f}s > {cap_s}s"
    assert elapsed_s > 0.0, "E-10 elapsed time did not advance"
    assert time_to_dead_letter_s is not None, "E-10 dead-letter conclusion not reached"
    assert _job_status(job_id) == "dead"


def test_e11_240s_hold_sampling_every_15s_with_elapsed_assertion() -> None:
    reset_collector_state()
    request_id = _single_page_request()
    job_id = _job_id_for_request(request_id)
    _set_job_state(
        job_id,
        status="in_progress",
        attempts=1,
        lease_seconds_offset=-120,
        last_error="E-11 forced recovery sample",
    )

    hold_s = 240
    interval_s = 15
    start = time.monotonic()
    recovery_latency_s: float | None = None
    samples: list[dict[str, Any]] = []
    while True:
        now_elapsed = time.monotonic() - start
        detail = _api_get_json("/v1/health/detail")
        status = _job_status(job_id)
        sweep_result = _run_sweep_now()
        samples.append(
            {
                "elapsed_s": round(now_elapsed, 3),
                "job_status": status,
                "health_dead": detail.get("dead"),
                "health_unloaded": detail.get("unloaded"),
                "sweep": sweep_result,
            }
        )
        if recovery_latency_s is None and status == "done":
            recovery_latency_s = now_elapsed
        if now_elapsed >= hold_s:
            break
        time.sleep(interval_s)

    elapsed_s = time.monotonic() - start
    print(f"E-11 recovery_latency_s={recovery_latency_s}")
    print(f"E-11 time_to_dead_letter_s=N/A")
    print(f"E-11 elapsed_s={elapsed_s:.3f}")
    write_evidence(
        "E-11",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"recovery_latency_s={recovery_latency_s}",
            f"elapsed_s={elapsed_s:.6f}",
            f"sample_count={len(samples)}",
            f"samples={json.dumps(samples, sort_keys=True)}",
        ],
    )
    assert elapsed_s >= hold_s, f"E-11 under-ran hold: {elapsed_s:.3f}s < {hold_s}s"
    assert elapsed_s > 0.0, "E-11 elapsed time did not advance"
    assert recovery_latency_s is not None, "E-11 recovery never observed during hold"
    assert _job_status(job_id) == "done"
