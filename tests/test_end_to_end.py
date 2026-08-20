"""Sprint 3 exit criteria — end-to-end against a LIVE local stack.

Preconditions (started by scripts/09_e2e.sh, or manually):
  - Cloud SQL Auth Proxy on 5432 with COLLECTOR_DSN / SENTINEL_MOCK_DSN
  - Mock Sentinel on http://127.0.0.1:8081
  - Request API on http://127.0.0.1:8080
  - One Procrastinate worker consuming queue ``sentinel`` (-c 1)
  - SENTINEL_URL pointing at the local mock (not Cloud Run ingress)

These tests are the Sprint 3 exit criteria expressed as code.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import psycopg
import pytest
from google.cloud import bigquery, storage

API_URL = os.environ.get("COLLECTOR_API_URL", "http://127.0.0.1:8080")
MOCK_URL = os.environ.get("MOCK_SENTINEL_URL", "http://127.0.0.1:8081")
POLL_TIMEOUT_S = 180
POLL_INTERVAL_S = 2.0


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'").strip('"')


_load_dotenv()


@pytest.fixture(scope="module")
def api() -> httpx.Client:
    with httpx.Client(base_url=API_URL, timeout=60.0) as client:
        try:
            r = client.get("/health")
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"API not healthy at {API_URL}: {exc}")
        try:
            httpx.get(f"{MOCK_URL}/health", timeout=10.0).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"Mock not healthy at {MOCK_URL}: {exc}")
        yield client


@pytest.fixture(scope="module")
def incident_ids() -> list[str]:
    dsn = os.environ.get("SENTINEL_MOCK_DSN")
    if not dsn:
        pytest.fail("SENTINEL_MOCK_DSN is required")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sentinel_incident ORDER BY id LIMIT 1000"
            )
            ids = [row[0] for row in cur.fetchall()]
    if len(ids) != 1000:
        pytest.fail(f"expected 1000 seeded incidents, got {len(ids)}")
    return ids


def _gcs_request_object_count(request_id: str) -> int:
    bucket_name = os.environ["RAW_BUCKET"]
    prefix = "raw/source=sentinel/"
    needle = f"request={request_id}/"
    client = storage.Client()
    return sum(
        1
        for blob in client.list_blobs(bucket_name, prefix=prefix)
        if needle in blob.name
    )


def _wait_for_done(api: httpx.Client, request_id: str) -> dict:
    deadline = time.time() + POLL_TIMEOUT_S
    last: dict = {}
    while time.time() < deadline:
        r = api.get(f"/v1/requests/{request_id}/counts")
        r.raise_for_status()
        last = r.json().get("counts") or {}
        if last.get("done", 0) == 20:
            return last
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"timeout after {POLL_TIMEOUT_S}s waiting for done==20; last counts={last}"
    )


def test_bad_requests(api: httpx.Client) -> None:
    r = api.post("/v1/collect", json={"source": "sentinel", "query_spec": {}})
    assert r.status_code == 400

    r = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"status": "open"}},
    )
    assert r.status_code == 400

    r = api.post(
        "/v1/collect",
        json={"source": "no-such-source", "query_spec": {"incident_ids": ["IN1"]}},
    )
    assert r.status_code == 400


def test_full_collection_run(api: httpx.Client, incident_ids: list[str]) -> None:
    r = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": incident_ids}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_pages"] == 20
    assert body["keys"] == 1000
    request_id = body["request_id"]

    _wait_for_done(api, request_id)

    dsn = os.environ["COLLECTOR_DSN"]
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # a) all 20 jobs done
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE status = 'done'),
                       count(*)
                FROM collector_job
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            done_n, total_n = cur.fetchone()
            assert total_n == 20 and done_n == 20

            # b) silent-failure check — raw landed, warehouse missed it
            cur.execute(
                """
                SELECT count(*)
                FROM collector_job
                WHERE request_id = %s::uuid
                  AND raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                """,
                (request_id,),
            )
            (silent_n,) = cur.fetchone()
            assert silent_n == 0

            # c) raw_manifest has exactly 20 rows for this request
            cur.execute(
                """
                SELECT count(*)
                FROM raw_manifest
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            (manifest_n,) = cur.fetchone()
            assert manifest_n == 20

    # d) GCS holds exactly 20 objects for this request under raw/source=sentinel/
    assert _gcs_request_object_count(request_id) == 20

    # e) thread explosion surviving the whole pipeline — the single most
    # convincing number in the demo. Scoped to this request's append batch.
    project = os.environ["PROJECT"]
    bq = bigquery.Client(project=project)
    row = list(
        bq.query(
            f"""
            SELECT
              count(*) AS n,
              count(DISTINCT id) AS n_ids
            FROM `{project}.sentinel_raw.incidents`
            WHERE _request_id = @rid
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("rid", "STRING", request_id),
                ]
            ),
        ).result()
    )[0]
    n = int(row["n"])
    n_ids = int(row["n_ids"])
    assert n_ids == 1000
    factor = n / n_ids if n_ids else 0.0
    assert 1.5 < factor < 3.5, f"expected ~2.5x thread explosion, got {factor:.3f}"


def test_idempotent_resubmit(api: httpx.Client, incident_ids: list[str]) -> None:
    first = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": incident_ids}},
    )
    assert first.status_code == 200, first.text
    first_id = first.json()["request_id"]
    _wait_for_done(api, first_id)
    assert _gcs_request_object_count(first_id) == 20

    second = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": incident_ids}},
    )
    assert second.status_code == 200, second.text
    second_id = second.json()["request_id"]
    assert second_id != first_id
    _wait_for_done(api, second_id)

    assert _gcs_request_object_count(first_id) == 20
    assert _gcs_request_object_count(second_id) == 20
