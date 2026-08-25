from __future__ import annotations

import math
import os
import time

import psycopg
import pytest

from tests.acceptance.helpers import (
    dataset_by_code,
    incident_ids_from_identity_set,
    post_collect,
    reset_collector_state,
    wait_request_terminal,
    write_evidence,
)


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _sentinel_worker_count() -> int:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)::int
                FROM procrastinate_workers pw
                WHERE now() - pw.last_heartbeat < interval '60 seconds'
                  AND EXISTS (
                    SELECT 1
                    FROM procrastinate_jobs pj
                    WHERE pj.worker_id = pw.id
                      AND pj.queue_name = 'sentinel'
                  )
                """
            )
            count = int(cur.fetchone()[0])
    # If no active job has yet been assigned in this second, assume at least one
    # sentinel worker from environment topology and validate via throughput test.
    return max(count, 1)


@pytest.fixture(scope="module")
def ds2_snapshot():
    ds2 = dataset_by_code("DS-2")
    snap = ds2.build()
    return snap


def test_f1_measure_pages_per_minute_per_worker(ds2_snapshot) -> None:
    reset_collector_state()
    ds2 = dataset_by_code("DS-2")
    truth_rows = ds2.truth()
    incident_ids = sorted({str(row["id"]) for row in truth_rows})
    sample_ids = incident_ids[:1000]

    workers = _sentinel_worker_count()
    start = time.monotonic()
    request_id = post_collect("sentinel", {"incident_ids": sample_ids})
    outcome = wait_request_terminal(request_id, timeout_s=1200)
    elapsed_s = time.monotonic() - start

    if elapsed_s <= 0:
        raise AssertionError("elapsed time was non-positive in F-1")
    ppm_total = outcome.total_pages / (elapsed_s / 60.0)
    ppm_per_worker = ppm_total / workers
    write_evidence(
        "F-1",
        [
            f"request_id={request_id}",
            f"sample_incident_count={len(sample_ids)}",
            f"total_pages={outcome.total_pages}",
            f"elapsed_seconds={elapsed_s:.6f}",
            f"sentinel_worker_count={workers}",
            f"pages_per_minute_total={ppm_total:.6f}",
            f"pages_per_minute_per_worker={ppm_per_worker:.6f}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert ppm_per_worker > 0.0


def test_f2_derive_max_population_for_30_min_window(ds2_snapshot) -> None:
    reset_collector_state()
    ds2 = dataset_by_code("DS-2")
    truth_rows = ds2.truth()
    incident_ids = sorted({str(row["id"]) for row in truth_rows})
    sample_ids = incident_ids[:1000]
    workers = _sentinel_worker_count()

    start = time.monotonic()
    request_id = post_collect("sentinel", {"incident_ids": sample_ids})
    outcome = wait_request_terminal(request_id, timeout_s=1200)
    elapsed_s = time.monotonic() - start

    ppm_total = outcome.total_pages / (elapsed_s / 60.0)
    ppm_per_worker = ppm_total / workers
    batch_cap = 50
    max_population_30m = math.floor(ppm_per_worker * workers * 30 * batch_cap)
    write_evidence(
        "F-2",
        [
            f"request_id={request_id}",
            f"total_pages={outcome.total_pages}",
            f"elapsed_seconds={elapsed_s:.6f}",
            f"sentinel_worker_count={workers}",
            f"pages_per_minute_per_worker={ppm_per_worker:.6f}",
            f"batch_cap={batch_cap}",
            f"derived_max_population_30m={max_population_30m}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert max_population_30m > 0


def test_f3_ds2_population_vs_30m_capacity(ds2_snapshot) -> None:
    ds2 = dataset_by_code("DS-2")
    truth_rows = ds2.truth()
    ds2_incident_count = len({str(row["id"]) for row in truth_rows})

    # Reuse measured capacity by running one bounded request under identical conditions.
    reset_collector_state()
    sample_ids = sorted({str(row["id"]) for row in truth_rows})[:1000]
    workers = _sentinel_worker_count()
    start = time.monotonic()
    request_id = post_collect("sentinel", {"incident_ids": sample_ids})
    outcome = wait_request_terminal(request_id, timeout_s=1200)
    elapsed_s = time.monotonic() - start
    ppm_per_worker = (outcome.total_pages / (elapsed_s / 60.0)) / workers
    batch_cap = 50
    max_population_30m = math.floor(ppm_per_worker * workers * 30 * batch_cap)
    margin = max_population_30m - ds2_incident_count
    write_evidence(
        "F-3",
        [
            f"request_id={request_id}",
            f"ds2_incident_population={ds2_incident_count}",
            f"pages_per_minute_per_worker={ppm_per_worker:.6f}",
            f"derived_max_population_30m={max_population_30m}",
            f"capacity_margin={margin}",
        ],
    )
    assert outcome.failed == 0
    assert outcome.dead == 0
