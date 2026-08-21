"""Sprint 4 exit gate — the three failure modes the collector must survive.

The happy path was proven in Sprint 3; these are what actually get demonstrated.

Preconditions (started by scripts/12_failures.sh, or manually):
  - Cloud SQL Auth Proxy on 5432 with COLLECTOR_DSN / SENTINEL_MOCK_DSN
  - Mock Sentinel on http://127.0.0.1:8081
  - Request API on http://127.0.0.1:8080
  - One maintenance worker (queue ``maintenance``)
  - SENTINEL_URL pointing at the local mock

Sentinel workers are started by the tests themselves so hard-kill recovery can
own the only consumer of the sentinel queue.

The same test file must pass locally and against Cloud Run. If a test needs
different assertions for the deployed stack, that is a signal the deployment
changed behaviour, and it should be investigated rather than accommodated.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest

# Same file locally and on Cloud Run — only the base URL / token change.
API_URL = os.environ.get("COLLECTOR_API_URL", "http://localhost:8080")
MOCK_URL = os.environ.get("MOCK_SENTINEL_URL", "http://127.0.0.1:8081")
POLL_INTERVAL_S = 2.0
RECOVERY_TIMEOUT_S = 240


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


def _auth_headers() -> dict[str, str]:
    token = os.environ.get("COLLECTOR_API_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _dsn() -> str:
    dsn = os.environ.get("COLLECTOR_DSN")
    if not dsn:
        pytest.fail("COLLECTOR_DSN is required")
    return dsn


def _gcs_request_object_count(request_id: str) -> int:
    from google.cloud import storage

    bucket_name = os.environ["RAW_BUCKET"]
    prefix = "raw/source=sentinel/"
    needle = f"request={request_id}/"
    client = storage.Client()
    return sum(
        1
        for blob in client.list_blobs(bucket_name, prefix=prefix)
        if needle in blob.name
    )


def _start_worker(queue: str, log_name: str) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd())
    env["PROCRASTINATE_APP"] = "collector.app.app"
    # Never inherit the internal Cloud Run mock URL from .env.
    env["SENTINEL_URL"] = "http://127.0.0.1:8081"
    log_path = Path("/tmp") / log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = log_path.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "procrastinate",
            "worker",
            "-q",
            queue,
            "-c",
            "1",
            "--delete-jobs",
            "never",
        ],
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        cwd=str(Path.cwd()),
    )
    proc._log_f = log_f  # type: ignore[attr-defined]
    time.sleep(2)
    if proc.poll() is not None:
        pytest.fail(f"worker on queue={queue} exited immediately; see {log_path}")
    return proc


def _hard_kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # TerminateProcess — no graceful cleanup, same idea as SIGKILL.
            proc.kill()
        else:
            os.kill(proc.pid, signal.SIGKILL)
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_f = getattr(proc, "_log_f", None)
        if log_f is not None:
            log_f.close()


def _stop_worker(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _hard_kill(proc)
    log_f = getattr(proc, "_log_f", None)
    if log_f is not None:
        try:
            log_f.close()
        except Exception:  # noqa: BLE001
            pass


def _run_sweep() -> dict:
    """Trigger one sweep immediately (same path as make sweep-now)."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from collector.app import app
    from collector.tasks import sweep

    async def main() -> dict:
        async with app.open_async():
            return await sweep(int(time.time()))

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["loop_factory"] = asyncio.SelectorEventLoop
    return asyncio.run(main(), **kwargs)


def _age_stalled_workers_and_leases(
    request_id: str | None = None,
    *,
    age_workers: bool = True,
) -> None:
    """Make Layer A/B see stranded work without waiting a full minute/lease.

    age_workers=False leaves live recovery workers alone (only expires leases).
    """
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            if age_workers:
                cur.execute(
                    """
                    UPDATE procrastinate_workers
                    SET last_heartbeat = now() - interval '120 seconds'
                    """
                )
            if request_id is not None:
                cur.execute(
                    """
                    UPDATE collector_job
                    SET lease_expires_at = now() - interval '1 second'
                    WHERE request_id = %s::uuid
                      AND status = 'in_progress'
                    """,
                    (request_id,),
                )
        conn.commit()


def _job_statuses(request_id: str) -> dict[str, int]:
    with psycopg.connect(_dsn()) as conn:
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
            return {status: count for status, count in cur.fetchall()}


def _wait_until(
    predicate,
    *,
    timeout_s: float,
    label: str,
) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(f"timeout after {timeout_s}s waiting for {label}; last={last!r}")


@pytest.fixture(scope="module")
def api() -> httpx.Client:
    with httpx.Client(
        base_url=API_URL, timeout=60.0, headers=_auth_headers()
    ) as client:
        try:
            client.get("/health").raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"API not healthy at {API_URL}: {exc}")
        try:
            httpx.get(
                f"{MOCK_URL}/health",
                headers=_auth_headers(),
                timeout=10.0,
            ).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"Mock not healthy at {MOCK_URL}: {exc}")
        # Clear any leftover killswitch / faults from prior demos.
        httpx.delete(
            f"{MOCK_URL}/admin/fault",
            headers=_auth_headers(),
            timeout=10.0,
        )
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


@pytest.fixture
def sentinel_worker():
    proc = _start_worker("sentinel", f"fail-sentinel-{uuid.uuid4().hex[:8]}.log")
    try:
        yield proc
    finally:
        _stop_worker(proc)


def test_hard_kill_recovery(api: httpx.Client, incident_ids: list[str]) -> None:
    worker = _start_worker("sentinel", "fail-hard-kill-worker.log")
    request_id = None
    recovery: subprocess.Popen[bytes] | None = None
    try:
        r = api.post(
            "/v1/collect",
            json={
                "source": "sentinel",
                "query_spec": {"incident_ids": incident_ids},
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_pages"] == 20
        request_id = body["request_id"]

        def ready_to_kill() -> bool:
            st = _job_statuses(request_id)
            return st.get("done", 0) >= 3 and st.get("in_progress", 0) >= 1

        _wait_until(ready_to_kill, timeout_s=180, label=">=3 done with one in_progress")

        _hard_kill(worker)
        worker = None  # already reaped

        statuses = _job_statuses(request_id)
        assert statuses.get("in_progress", 0) >= 1, statuses

        # Age the killed worker's heartbeat + expire leases once, BEFORE the
        # replacement worker starts. Do not re-expire leases in the wait loop —
        # that thrash-resets pages the recovery worker is actively running.
        _age_stalled_workers_and_leases(request_id, age_workers=True)

        recovery = _start_worker("sentinel", "fail-hard-kill-recovery.log")
        sweep_result = _run_sweep()
        assert isinstance(sweep_result, dict)

        deadline = time.time() + RECOVERY_TIMEOUT_S
        last_statuses: dict[str, int] = {}
        stagnant = 0
        prev_done = -1
        while time.time() < deadline:
            last_statuses = _job_statuses(request_id)
            done_n = last_statuses.get("done", 0)
            if done_n == 20 and sum(last_statuses.values()) == 20:
                break
            if done_n == prev_done:
                stagnant += 1
            else:
                stagnant = 0
                prev_done = done_n
            # If progress stalls, one more sweep for orphans — still no lease thrash.
            if stagnant >= 15:
                _run_sweep()
                stagnant = 0
            time.sleep(POLL_INTERVAL_S)
        else:
            pytest.fail(
                f"recovery incomplete in {RECOVERY_TIMEOUT_S}s; "
                f"statuses={last_statuses}"
            )

        assert last_statuses.get("done") == 20
        # Deterministic object naming means a redone page overwrites rather
        # than duplicating — this is the assertion that proves it.
        assert _gcs_request_object_count(request_id) == 20

        recon = api.get("/v1/reconcile", params={"minutes": 0})
        recon.raise_for_status()
        assert recon.json()["unloaded"] == 0
    finally:
        if worker is not None:
            _stop_worker(worker)
        _stop_worker(recovery)


def test_source_failure_retries(
    api: httpx.Client,
    incident_ids: list[str],
    sentinel_worker: subprocess.Popen[bytes],
) -> None:
    del sentinel_worker  # fixture keeps the worker alive
    bad_id = incident_ids[0]
    # One page only — keeps the fault isolated.
    page_ids = incident_ids[:50]

    fr = httpx.post(
        f"{MOCK_URL}/admin/fault/{bad_id}",
        headers=_auth_headers(),
        timeout=10.0,
    )
    fr.raise_for_status()
    try:
        r = api.post(
            "/v1/collect",
            json={
                "source": "sentinel",
                "query_spec": {"incident_ids": page_ids},
            },
        )
        assert r.status_code == 200, r.text
        request_id = r.json()["request_id"]

        def attempts_above_one() -> bool:
            with psycopg.connect(_dsn()) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT attempts, status
                        FROM collector_job
                        WHERE request_id = %s::uuid
                        """,
                        (request_id,),
                    )
                    row = cur.fetchone()
            if row is None:
                return False
            attempts, status = row
            return attempts > 1 and status != "dead"

        _wait_until(
            attempts_above_one,
            timeout_s=90,
            label="attempts > 1 and not dead",
        )

        with psycopg.connect(_dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attempts, status
                    FROM collector_job
                    WHERE request_id = %s::uuid
                    """,
                    (request_id,),
                )
                attempts, status = cur.fetchone()
        assert attempts > 1
        assert status != "dead"
        # This proves Procrastinate's RetryStrategy(max_attempts=3,
        # exponential_wait=4) is actually wired, not just declared.
    finally:
        httpx.delete(
            f"{MOCK_URL}/admin/fault",
            headers=_auth_headers(),
            timeout=10.0,
        ).raise_for_status()

    def page_done() -> bool:
        return _job_statuses(request_id).get("done", 0) == 1

    _wait_until(page_done, timeout_s=120, label="faulted page reaches done")


def test_reconcile_detects_silent_gap(
    api: httpx.Client,
    incident_ids: list[str],
    sentinel_worker: subprocess.Popen[bytes],
) -> None:
    del sentinel_worker
    # Prior hard-kill runs can leave a global silent gap; clear those so this
    # test's reconcile==0 baseline is about the collection we are about to run.
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_job
                SET loaded_at = coalesce(loaded_at, raw_written_at, now())
                WHERE raw_written_at IS NOT NULL
                  AND loaded_at IS NULL
                """
            )
        conn.commit()

    # Four pages so page_no=3 exists.
    ids = incident_ids[:200]
    r = api.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": ids}},
    )
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]

    def all_done() -> bool:
        st = _job_statuses(request_id)
        return st.get("done", 0) == 4 and sum(st.values()) == 4

    _wait_until(all_done, timeout_s=180, label="4-page collection done")

    recon = api.get("/v1/reconcile", params={"minutes": 0})
    recon.raise_for_status()
    assert recon.json()["unloaded"] == 0

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text, loaded_at, raw_written_at
                FROM collector_job
                WHERE request_id = %s::uuid AND page_no = 3
                """,
                (request_id,),
            )
            job_id, loaded_at, raw_written_at = cur.fetchone()
            assert loaded_at is not None and raw_written_at is not None

            # Simulate the silent failure: raw landed, warehouse missed it.
            cur.execute(
                """
                UPDATE collector_job
                SET loaded_at = NULL,
                    raw_written_at = now() - interval '1 hour'
                WHERE request_id = %s::uuid AND page_no = 3
                """,
                (request_id,),
            )
        conn.commit()

    recon = api.get("/v1/reconcile", params={"minutes": 0})
    recon.raise_for_status()
    body = recon.json()
    # This is the check called non-negotiable in review. Raw landed in GCS,
    # the warehouse missed it, and no error was raised anywhere. Only this
    # query finds it.
    assert body["unloaded"] == 1
    assert any(row["job_id"] == job_id for row in body["rows"])

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_job
                SET loaded_at = %s,
                    raw_written_at = %s
                WHERE job_id = %s::uuid
                """,
                (loaded_at, raw_written_at, job_id),
            )
        conn.commit()

    recon = api.get("/v1/reconcile", params={"minutes": 0})
    recon.raise_for_status()
    assert recon.json()["unloaded"] == 0


def test_dead_letter(
    api: httpx.Client,
    incident_ids: list[str],
    sentinel_worker: subprocess.Popen[bytes],
) -> None:
    del sentinel_worker
    # Force a job past max attempts via lease expiry + attempts >= 5, then
    # let the sweeper dead-letter it (poison page must stop retrying).
    r = api.post(
        "/v1/collect",
        json={
            "source": "sentinel",
            "query_spec": {"incident_ids": incident_ids[:50]},
        },
    )
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]

    _wait_until(
        lambda: sum(_job_statuses(request_id).values()) == 1,
        timeout_s=60,
        label="job row exists",
    )

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_job
                SET status = 'in_progress',
                    attempts = 5,
                    lease_expires_at = now() - interval '1 minute',
                    last_error = 'injected poison for dead-letter test',
                    updated_at = now()
                WHERE request_id = %s::uuid
                RETURNING job_id::text
                """,
                (request_id,),
            )
            (job_id,) = cur.fetchone()
        conn.commit()

    result = _run_sweep()
    assert result["rows_dead_lettered"] >= 1

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM collector_job WHERE job_id = %s::uuid",
                (job_id,),
            )
            (status,) = cur.fetchone()
    assert status == "dead"

    # A poison page must stop retrying rather than cycling forever, and it
    # must be visible to a human.
    dl = api.get("/v1/dead-letter")
    dl.raise_for_status()
    body = dl.json()
    assert body["dead"] >= 1
    assert any(row["job_id"] == job_id for row in body["rows"])


def test_sweeper_does_not_double_recover(
    api: httpx.Client,
    incident_ids: list[str],
) -> None:
    # No sentinel worker: we need the deferred/retried job to stay live so we
    # can count todo/doing rows after two sweeps.
    r = api.post(
        "/v1/collect",
        json={
            "source": "sentinel",
            "query_spec": {"incident_ids": incident_ids[:50]},
        },
    )
    assert r.status_code == 200, r.text
    request_id = r.json()["request_id"]

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text
                FROM collector_job
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            (job_id,) = cur.fetchone()

            # Strand our row (Layer B) and leave a doing orphan (Layer A).
            cur.execute(
                """
                UPDATE collector_job
                SET status = 'in_progress',
                    attempts = 1,
                    lease_expires_at = now() - interval '1 hour',
                    updated_at = now()
                WHERE job_id = %s::uuid
                """,
                (job_id,),
            )
            cur.execute(
                """
                UPDATE procrastinate_jobs
                SET status = 'doing',
                    worker_id = NULL
                WHERE args->>'job_id' = %s
                  AND status IN ('todo', 'doing')
                """,
                (job_id,),
            )
        conn.commit()

    _run_sweep()
    _run_sweep()

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)::int
                FROM procrastinate_jobs
                WHERE args->>'job_id' = %s
                  AND status IN ('todo', 'doing')
                """,
                (job_id,),
            )
            (live_n,) = cur.fetchone()

    # Guards the double-recovery trap — Layer A's retry_job and Layer B's
    # defer must not both fire for the same page.
    assert live_n == 1, f"expected exactly one live job, got {live_n}"
