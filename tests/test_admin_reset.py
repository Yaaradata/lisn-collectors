"""Admin reset tests — unit guards + live-stack verification.

Live tests read ``COLLECTOR_API_URL`` (and optional ``COLLECTOR_API_TOKEN``).
They require Cloud SQL proxy (``COLLECTOR_DSN`` / ``SENTINEL_MOCK_DSN``),
workers consuming ``sentinel``, ``ALLOW_ADMIN_RESET=1``, and scoped IAM from
``make grant-admin-reset``.

Unit tests at the top do not need a live stack.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import psycopg
import pytest
from google.cloud import bigquery, storage

# collector.app reads COLLECTOR_DSN at import time (unit tests only).
os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)

CONFIRM = "reset-collector-data"
POLL_TIMEOUT_S = 180
POLL_INTERVAL_S = 1.0

# Both routes must behave identically — every behavioural test is parametrised.
RESET_ROUTES = ("delete", "post")


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

API_URL = os.environ.get("COLLECTOR_API_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# Unit guards (no live stack)
# ---------------------------------------------------------------------------


def _unit_client():
    from fastapi.testclient import TestClient

    from collector.api import api

    return TestClient(api)


def _unit_call(
    client: Any,
    route: str,
    *,
    confirm: str | None = CONFIRM,
    dry_run: bool | None = True,
    force: bool = False,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """Call DELETE collector-data or deprecated POST reset via TestClient."""
    if route == "delete":
        params: dict[str, Any] = {}
        if confirm is not None:
            params["confirm"] = confirm
        if dry_run is not None:
            params["dry_run"] = dry_run
        if force:
            params["force"] = force
        return client.delete("/v1/admin/collector-data", params=params)
    if json_body is not None:
        return client.post("/v1/admin/reset", json=json_body)
    body: dict[str, Any] = {"force": force}
    if confirm is not None:
        body["confirm"] = confirm
    if dry_run is not None:
        body["dry_run"] = dry_run
    return client.post("/v1/admin/reset", json=body)


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_unit_disabled_without_env(monkeypatch: Any, route: str) -> None:
    monkeypatch.delenv("ALLOW_ADMIN_RESET", raising=False)
    r = _unit_call(_unit_client(), route, confirm=CONFIRM, dry_run=True)
    assert r.status_code == 403


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_unit_bad_confirm_does_not_call_run(monkeypatch: Any, route: str) -> None:
    monkeypatch.setenv("ALLOW_ADMIN_RESET", "1")
    with patch("collector.api.run_reset") as run:
        r = _unit_call(_unit_client(), route, confirm="yes", dry_run=False)
    assert r.status_code == 400
    run.assert_not_called()


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_unit_dry_run_defaults_true(monkeypatch: Any, route: str) -> None:
    monkeypatch.setenv("ALLOW_ADMIN_RESET", "1")
    with patch(
        "collector.api.run_reset",
        return_value={"dry_run": True, "cleared": {}, "preserved": {}, "warnings": []},
    ) as run:
        if route == "delete":
            # Omit dry_run query param — server default must be true.
            r = _unit_client().delete(
                "/v1/admin/collector-data",
                params={"confirm": CONFIRM},
            )
        else:
            r = _unit_client().post(
                "/v1/admin/reset",
                json={"confirm": CONFIRM},
            )
    assert r.status_code == 200
    run.assert_called_once()
    assert run.call_args.kwargs["dry_run"] is True


def test_unit_allowlist_is_explicit() -> None:
    from collector.admin_reset import RESET_BQ_TABLES, RESET_CLOUD_SQL_TABLES

    assert "procrastinate_workers" not in RESET_CLOUD_SQL_TABLES
    assert RESET_BQ_TABLES == [
        "sentinel_raw.incidents",
        "sentinel_raw.discovered_ids",
    ]


def test_unit_admin_state_is_readonly(monkeypatch: Any) -> None:
    monkeypatch.delenv("ALLOW_ADMIN_RESET", raising=False)
    with patch(
        "collector.api.collect_live_state",
        return_value={"cloud_sql": {}, "warnings": []},
    ) as state:
        r = _unit_client().get("/v1/admin/state")
    assert r.status_code == 200
    state.assert_called_once()


# ---------------------------------------------------------------------------
# Live stack helpers
# ---------------------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _require_live() -> None:
    if not API_URL:
        pytest.skip("COLLECTOR_API_URL not set")
    for key in ("COLLECTOR_DSN", "SENTINEL_MOCK_DSN", "PROJECT", "RAW_BUCKET"):
        if not os.environ.get(key):
            pytest.skip(f"{key} required for live admin-reset tests")


@pytest.fixture(scope="module")
def api() -> httpx.Client:
    _require_live()
    with httpx.Client(
        base_url=API_URL, timeout=120.0, headers=_auth_headers()
    ) as client:
        try:
            r = client.get("/health")
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"API not healthy at {API_URL}: {exc}")
        probe = client.delete(
            "/v1/admin/collector-data",
            params={"confirm": CONFIRM, "dry_run": True},
        )
        if probe.status_code == 403:
            pytest.skip("ALLOW_ADMIN_RESET is not enabled on this API")
        yield client


@pytest.fixture(scope="module")
def sample_incident_ids() -> list[str]:
    _require_live()
    with psycopg.connect(os.environ["SENTINEL_MOCK_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sentinel_incident ORDER BY id LIMIT 100"
            )
            ids = [row[0] for row in cur.fetchall()]
    if len(ids) < 50:
        pytest.skip(f"need ≥50 seeded incidents, got {len(ids)}")
    return ids


def _sql_count(table: str) -> int:
    with psycopg.connect(os.environ["COLLECTOR_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*)::int FROM {table}")  # noqa: S608
            return int(cur.fetchone()[0])


def _sql_counts() -> dict[str, int]:
    return {
        "collector_job": _sql_count("collector_job"),
        "collector_request": _sql_count("collector_request"),
        "raw_manifest": _sql_count("raw_manifest"),
        "procrastinate_workers": _sql_count("procrastinate_workers"),
    }


def _gcs_raw_count() -> int:
    bucket = os.environ["RAW_BUCKET"]
    client = storage.Client(project=os.environ.get("PROJECT"))
    return sum(
        1
        for b in client.list_blobs(bucket, prefix="raw/")
        if b.name.startswith("raw/")
    )


def _bq_count(rel: str) -> int:
    project = os.environ["PROJECT"]
    region = os.environ.get("REGION", "asia-south1")
    client = bigquery.Client(project=project, location=region)
    table = f"{project}.{rel}"
    row = list(client.query(f"SELECT count(*) AS n FROM `{table}`").result())[0]
    return int(row.n)


def _sample_counts() -> tuple[int, int]:
    with psycopg.connect(os.environ["SENTINEL_MOCK_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*)::int FROM sentinel_incident")
            incidents = int(cur.fetchone()[0])
            cur.execute("SELECT count(*)::int FROM sentinel_thread")
            threads = int(cur.fetchone()[0])
    return incidents, threads


def _wait_request_idle(api: httpx.Client, request_id: str) -> dict[str, int]:
    deadline = time.time() + POLL_TIMEOUT_S
    last: dict[str, int] = {}
    while time.time() < deadline:
        r = api.get(f"/v1/requests/{request_id}/counts")
        r.raise_for_status()
        last = r.json().get("counts") or {}
        pending = int(last.get("pending", 0)) + int(last.get("in_progress", 0))
        if pending == 0:
            return last
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"request {request_id} still open after {POLL_TIMEOUT_S}s: {last}")


def _collect(api: httpx.Client, ids: list[str]) -> str:
    r = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
    )
    assert r.status_code == 200, r.text
    return str(r.json()["request_id"])


def _collect_discovery(api: httpx.Client, *, limit: int = 50) -> str:
    now = datetime.now(timezone.utc)
    r = api.post(
        "/v1/collect",
        json={
            "source": "sentinel_discovery",
            "query_spec": {
                "updated_from": (now - timedelta(days=14)).isoformat(),
                "updated_to": now.isoformat(),
                "limit": limit,
            },
        },
    )
    assert r.status_code == 200, r.text
    return str(r.json()["request_id"])


def _reset(
    api: httpx.Client,
    *,
    dry_run: bool,
    force: bool = False,
    route: str = "delete",
) -> httpx.Response:
    if route == "delete":
        return api.delete(
            "/v1/admin/collector-data",
            params={
                "confirm": CONFIRM,
                "dry_run": dry_run,
                "force": force,
            },
        )
    return api.post(
        "/v1/admin/reset",
        json={"confirm": CONFIRM, "dry_run": dry_run, "force": force},
    )


def _snapshot() -> dict[str, int]:
    return {
        **_sql_counts(),
        "gcs_raw": _gcs_raw_count(),
        "bq_incidents": _bq_count("sentinel_raw.incidents"),
        "bq_discovered": _bq_count("sentinel_raw.discovered_ids"),
    }


def _cloud_run_failed_executions() -> list[str]:
    """Return Failed execution names for collector jobs, if gcloud is available."""
    if os.environ.get("DEPLOY_SURFACE") not in ("jobs", "worker-pools"):
        return []
    region = os.environ.get("REGION", "asia-south1")
    project = os.environ["PROJECT"]
    failed: list[str] = []
    for job in ("col-sentinel", "col-sentinel-discovery", "col-maintenance"):
        try:
            out = subprocess.check_output(
                [
                    "gcloud",
                    "run",
                    "jobs",
                    "executions",
                    "list",
                    f"--job={job}",
                    f"--project={project}",
                    f"--region={region}",
                    "--limit=5",
                    "--format=json",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue
        try:
            rows = json.loads(out or "[]")
        except json.JSONDecodeError:
            continue
        for row in rows:
            name = row.get("metadata", {}).get("name") or row.get("name") or ""
            conditions = (row.get("status") or {}).get("conditions") or []
            for cond in conditions:
                if (
                    cond.get("type") == "Completed"
                    and cond.get("status") == "False"
                    and "Fail" in str(cond.get("reason", ""))
                ):
                    failed.append(name)
                    break
            # Also catch explicit Failed condition name used by some SDKs.
            for cond in conditions:
                if cond.get("type") == "Failed" and cond.get("status") == "True":
                    failed.append(name)
                    break
    return failed


# ---------------------------------------------------------------------------
# Live tests (parametrised over DELETE + deprecated POST)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_requires_confirm_string(api: httpx.Client, route: str) -> None:
    before = _sql_count("collector_job")
    if route == "delete":
        r = api.delete(
            "/v1/admin/collector-data",
            params={"confirm": "nope", "dry_run": "false"},
        )
        r2 = api.delete(
            "/v1/admin/collector-data",
            params={"dry_run": "false"},
        )
    else:
        r = api.post(
            "/v1/admin/reset",
            json={"confirm": "nope", "dry_run": False},
        )
        r2 = api.post("/v1/admin/reset", json={"dry_run": False})
    assert r.status_code == 400, r.text
    # missing confirm → 422 validation
    assert r2.status_code in (400, 422), r2.text
    assert _sql_count("collector_job") == before


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_dry_run_changes_nothing(
    api: httpx.Client, sample_incident_ids: list[str], route: str
) -> None:
    rid = _collect(api, sample_incident_ids[:50])
    _wait_request_idle(api, rid)
    before = _snapshot()
    assert before["collector_job"] > 0

    r = _reset(api, dry_run=True, route=route)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    cleared = body["cleared"]
    assert int(cleared["collector_job"]["before"]) > 0
    assert int(cleared.get("gcs_objects", {}).get("before") or 0) >= 0

    after = _snapshot()
    assert after == before


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_preserves_sample_data(api: httpx.Client, route: str) -> None:
    """This is the most important test here.

    Everything else is recoverable by re-running a collection; the sample data
    is not. A reset that reaches sentinel_mock has destroyed the demo fixture.
    """
    before = _sample_counts()
    assert before[0] > 0 and before[1] > before[0], f"empty seed: {before}"

    r = _reset(api, dry_run=False, force=True, route=route)
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is False

    after = _sample_counts()
    assert after == before, f"sample data changed across reset: {before} → {after}"


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_preserves_live_workers(api: httpx.Client, route: str) -> None:
    """We killed col-maintenance this way once with a foreign key violation.

    Truncating procrastinate_workers while a Cloud Run job was live made the
    next fetch_job write worker_id=N against a missing row and the container
    exited(1). This test exists so that cannot happen again: workers must still
    have rows after reset, and no collector execution should flip to Failed.
    """
    workers_before = _sql_count("procrastinate_workers")
    assert workers_before > 0, "no procrastinate_workers rows — start workers first"
    failed_before = set(_cloud_run_failed_executions())

    r = _reset(api, dry_run=False, force=True, route=route)
    assert r.status_code == 200, r.text

    workers_after = _sql_count("procrastinate_workers")
    assert workers_after > 0, "procrastinate_workers emptied by reset"
    # Allow new registrations but never zero.
    preserved = r.json().get("preserved") or {}
    assert int(preserved.get("procrastinate_workers") or 0) > 0

    time.sleep(5)
    failed_after = set(_cloud_run_failed_executions())
    new_failed = failed_after - failed_before
    assert not new_failed, f"Cloud Run executions newly Failed: {new_failed}"


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_clears_collector_data(
    api: httpx.Client, sample_incident_ids: list[str], route: str
) -> None:
    rid = _collect(api, sample_incident_ids[:50])
    _wait_request_idle(api, rid)
    assert _sql_count("collector_job") > 0
    assert _gcs_raw_count() > 0

    r = _reset(api, dry_run=False, force=True, route=route)
    assert r.status_code == 200, r.text

    assert _sql_count("collector_job") == 0
    assert _sql_count("collector_request") == 0
    assert _sql_count("raw_manifest") == 0
    assert _gcs_raw_count() == 0
    assert _bq_count("sentinel_raw.incidents") == 0
    assert _bq_count("sentinel_raw.discovered_ids") == 0


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_refuses_when_work_in_flight(
    api: httpx.Client, sample_incident_ids: list[str], route: str
) -> None:
    # Large enough to fan out across several pages while workers are busy.
    ids = sample_incident_ids[:100]
    r = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
    )
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]

    saw_in_progress = False
    deadline = time.time() + 30
    reset_resp: httpx.Response | None = None
    before: dict[str, int] = {}
    while time.time() < deadline:
        with psycopg.connect(os.environ["COLLECTOR_DSN"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*)::int FROM collector_job
                    WHERE request_id = %s::uuid AND status = 'in_progress'
                    """,
                    (request_id,),
                )
                n = int(cur.fetchone()[0])
        if n > 0:
            saw_in_progress = True
            before = _snapshot()
            reset_resp = _reset(api, dry_run=False, force=False, route=route)
            break
        time.sleep(0.1)

    if not saw_in_progress:
        # Workers finished before we observed in_progress — pin one row so the
        # 409 path is still exercised against a real request.
        with psycopg.connect(os.environ["COLLECTOR_DSN"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE collector_job
                    SET status = 'in_progress',
                        lease_expires_at = now() + interval '15 minutes'
                    WHERE job_id = (
                      SELECT job_id FROM collector_job
                      WHERE request_id = %s::uuid
                      ORDER BY page_no
                      LIMIT 1
                    )
                    """,
                    (request_id,),
                )
            conn.commit()
        before = _snapshot()
        reset_resp = _reset(api, dry_run=False, force=False, route=route)

    assert reset_resp is not None
    assert reset_resp.status_code == 409, reset_resp.text
    detail = reset_resp.json()["detail"]
    assert int(detail.get("in_progress") or 0) >= 1

    after = _snapshot()
    assert after["collector_job"] == before["collector_job"]
    assert after["collector_request"] == before["collector_request"]
    assert after["raw_manifest"] == before["raw_manifest"]
    assert after["gcs_raw"] == before["gcs_raw"]

    # Clean up so later tests are not stuck behind a fake in_progress lease.
    _reset(api, dry_run=False, force=True, route=route)


@pytest.mark.parametrize("route", RESET_ROUTES)
def test_schema_survives(
    api: httpx.Client, sample_incident_ids: list[str], route: str
) -> None:
    r = _reset(api, dry_run=False, force=True, route=route)
    assert r.status_code == 200, r.text

    project = os.environ["PROJECT"]
    region = os.environ.get("REGION", "asia-south1")
    bq = bigquery.Client(project=project, location=region)
    table = bq.get_table(f"{project}.sentinel_raw.incidents")
    assert table.table_id == "incidents"
    part = table.time_partitioning
    assert part is not None
    assert part.field == "_ingested_at"
    cluster = table.clustering_fields or []
    assert "id" in cluster

    rid = _collect(api, sample_incident_ids[:50])
    _wait_request_idle(api, rid)

    row = list(
        bq.query(
            f"""
            SELECT count(*) AS n
            FROM `{project}.sentinel_raw.incidents`
            WHERE _request_id = @rid
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("rid", "STRING", rid),
                ]
            ),
        ).result()
    )[0]
    assert int(row.n) > 0, "collection after reset wrote no BigQuery rows"


def test_full_reset_leaves_only_sample_data(
    api: httpx.Client, sample_incident_ids: list[str]
) -> None:
    """This test exists because the endpoint once reported success while
    leaving GCS and BigQuery populated.

    It does NOT trust the API response body for clearance. Every assertion
    after DELETE reads Cloud SQL / GCS / BigQuery directly.
    """
    # 1) Real collection so all four stores hold data
    rid = _collect(api, sample_incident_ids[:50])
    _wait_request_idle(api, rid)
    rid_disc = _collect_discovery(api, limit=50)
    _wait_request_idle(api, rid_disc)

    # 2) Prove all four stores hold data (direct reads).
    assert _sql_count("collector_job") > 0
    assert _sql_count("collector_request") > 0
    assert _sql_count("raw_manifest") > 0
    assert _gcs_raw_count() > 0
    assert _bq_count("sentinel_raw.incidents") > 0
    assert _bq_count("sentinel_raw.discovered_ids") > 0

    workers_before = _sql_count("procrastinate_workers")
    assert workers_before > 0
    incidents_before, threads_before = _sample_counts()
    assert incidents_before > 0 and threads_before > incidents_before

    # 3) Real DELETE — body is logged but never trusted for clearance
    r = _reset(api, dry_run=False, force=True, route="delete")
    assert r.status_code == 200, r.text
    body = r.json()
    # success flag must reflect warnings; still do not trust cleared.*.after
    assert body.get("success") is True, body.get("warnings")

    # 4) Direct reads — ignore API cleared.*.after
    assert _sql_count("collector_request") == 0
    assert _sql_count("collector_job") == 0
    assert _sql_count("raw_manifest") == 0
    with psycopg.connect(os.environ["COLLECTOR_DSN"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*)::int FROM procrastinate_jobs WHERE status <> 'doing'"
            )
            assert int(cur.fetchone()[0]) == 0
    assert _gcs_raw_count() == 0
    assert _bq_count("sentinel_raw.incidents") == 0
    assert _bq_count("sentinel_raw.discovered_ids") == 0

    # 5) Sample data survived (direct sentinel_mock reads — unchanged vs before)
    after_sample = _sample_counts()
    assert after_sample == (incidents_before, threads_before)

    # 6) Workers still registered
    assert _sql_count("procrastinate_workers") > 0

    # 7) Schema survives + subsequent collection lands
    project = os.environ["PROJECT"]
    region = os.environ.get("REGION", "asia-south1")
    bq = bigquery.Client(project=project, location=region)
    table = bq.get_table(f"{project}.sentinel_raw.incidents")
    assert table.time_partitioning is not None
    assert table.time_partitioning.field == "_ingested_at"

    rid2 = _collect(api, sample_incident_ids[:20])
    _wait_request_idle(api, rid2)
    row = list(
        bq.query(
            f"""
            SELECT count(*) AS n
            FROM `{project}.sentinel_raw.incidents`
            WHERE _request_id = @rid
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("rid", "STRING", rid2),
                ]
            ),
            location=region,
        ).result()
    )[0]
    assert int(row.n) > 0
