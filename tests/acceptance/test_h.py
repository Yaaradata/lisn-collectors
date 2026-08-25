from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from google.cloud import bigquery

from tests.acceptance.datasets import DATASETS


UTC = timezone.utc


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _mock_dsn() -> str:
    return os.environ["SENTINEL_MOCK_DSN"]


def _project() -> str:
    return os.environ["PROJECT"].strip()


def _ensure_adc() -> None:
    payload = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")
    if not payload:
        return
    path = Path("/tmp/gcp-adc.json")
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


def _post_collect(source: str, query_spec: dict[str, object]) -> str:
    body = {"source": source, "query_spec": query_spec}
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/collect",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return str(payload["request_id"])


@dataclass
class JobObservation:
    queue_status: str | None
    queue_name: str | None
    worker_id: int | None
    job_status: str
    attempts: int


def _fetch_job_observation(request_id: str) -> JobObservation:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pj.status, pj.queue_name, pj.worker_id,
                       cj.status, cj.attempts
                FROM collector_job cj
                LEFT JOIN procrastinate_jobs pj
                  ON pj.args->>'job_id' = cj.job_id::text
                WHERE cj.request_id = %s::uuid
                ORDER BY cj.created_at ASC
                LIMIT 1
                """,
                (request_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise AssertionError(f"no collector_job for request_id={request_id}")
    return JobObservation(
        queue_status=row[0],
        queue_name=row[1],
        worker_id=row[2],
        job_status=row[3],
        attempts=int(row[4]),
    )


def _wait_for_job_terminal(request_id: str, timeout_s: int = 120) -> JobObservation:
    deadline = time.monotonic() + timeout_s
    last: JobObservation | None = None
    while time.monotonic() < deadline:
        last = _fetch_job_observation(request_id)
        if last.job_status in {"done", "dead", "failed"}:
            return last
        time.sleep(2)
    assert last is not None
    raise AssertionError(f"request {request_id} not terminal within {timeout_s}s: {last}")


def _bq_discovered_ids(request_id: str) -> list[str]:
    _ensure_adc()
    client = bigquery.Client(project=_project())
    query = f"""
        SELECT DISTINCT incident_id
        FROM `{_project()}.sentinel_raw.discovered_ids`
        WHERE _request_id = @rid
        ORDER BY incident_id
    """
    rows = list(
        client.query(
            query,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
            ),
        ).result()
    )
    return [str(r["incident_id"]) for r in rows if r["incident_id"] is not None]


def _mock_truth_discovery(start: datetime, end: datetime) -> list[str]:
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM sentinel_incident
                WHERE updated_on >= %s
                  AND updated_on <= %s
                ORDER BY id
                """,
                (start, end),
            )
            return [r[0] for r in cur.fetchall()]


def _ds11_window_bounds() -> tuple[datetime, datetime]:
    ds11 = next(d for d in DATASETS if d.code == "DS-11")
    snap = ds11.build()
    return snap.min_updated_on, snap.max_updated_on


def _window_iso(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "updated_from": start.isoformat().replace("+00:00", "Z"),
        "updated_to": end.isoformat().replace("+00:00", "Z"),
        "limit": 1000,
    }


def test_h0_discovery_runs_at_all() -> None:
    start, end = _ds11_window_bounds()
    one_hour_end = start + timedelta(hours=1)
    request_id = _post_collect("sentinel_discovery", _window_iso(start, one_hour_end))

    picked_up = False
    deadline = time.monotonic() + 60
    last = _fetch_job_observation(request_id)
    while time.monotonic() < deadline:
        last = _fetch_job_observation(request_id)
        if last.queue_status == "doing" or last.worker_id is not None or last.job_status == "in_progress":
            picked_up = True
            break
        time.sleep(2)
    assert picked_up, f"H-0 blocking: request {request_id} sat unpicked; last={last}"


def test_h1_discovery_finds_everything_in_window() -> None:
    start, end = _ds11_window_bounds()
    window_end = start + timedelta(hours=1)
    truth = _mock_truth_discovery(start, window_end)
    request_id = _post_collect("sentinel_discovery", _window_iso(start, window_end))
    terminal = _wait_for_job_terminal(request_id)
    assert terminal.job_status == "done"
    discovered = _bq_discovered_ids(request_id)
    assert discovered == truth


def test_h2_contiguous_windows_lose_nothing() -> None:
    start, end = _ds11_window_bounds()
    total_hours = int((end - start).total_seconds() // 3600)
    assert total_hours == 6
    truth_full = _mock_truth_discovery(start, end)
    union: set[str] = set()
    for i in range(6):
        w_start = start + timedelta(hours=i)
        w_end = w_start + timedelta(hours=1)
        request_id = _post_collect("sentinel_discovery", _window_iso(w_start, w_end))
        terminal = _wait_for_job_terminal(request_id)
        assert terminal.job_status == "done"
        union.update(_bq_discovered_ids(request_id))
    assert sorted(union) == truth_full


def test_h3_gap_window_loss_is_visible_in_measurement() -> None:
    start, end = _ds11_window_bounds()
    truth_full = _mock_truth_discovery(start, end)
    union: set[str] = set()
    for i in [0, 1, 3, 4, 5]:
        w_start = start + timedelta(hours=i)
        w_end = w_start + timedelta(hours=1)
        request_id = _post_collect("sentinel_discovery", _window_iso(w_start, w_end))
        terminal = _wait_for_job_terminal(request_id)
        assert terminal.job_status == "done"
        union.update(_bq_discovered_ids(request_id))
    missing = sorted(set(truth_full) - union)
    assert len(missing) > 0
    # Silent-loss shape check: no failed/dead status for these requests.
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM collector_job
                WHERE status IN ('failed', 'dead')
                  AND source = 'sentinel_discovery'
                """
            )
            failed_or_dead = int(cur.fetchone()[0])
    assert failed_or_dead == 0
