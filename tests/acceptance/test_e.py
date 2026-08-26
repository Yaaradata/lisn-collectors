from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from collector.app import app as procrastinate_app
from collector.tasks import sweep
from tests.acceptance.helpers import (
    bq_identity_set,
    dataset_by_code,
    incident_ids_from_identity_set,
    post_collect,
    post_collect_detailed,
    reset_collector_state,
    wait_request_terminal,
    write_evidence,
)

UTC = timezone.utc


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _api_get_json(path: str) -> dict[str, Any]:
    req = urllib.request.Request(f"http://127.0.0.1:8080{path}", method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _mock_call(method: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:8081{path}",
        method=method,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload_out = json.loads(raw)
        except json.JSONDecodeError:
            payload_out = {"raw": raw}
        return int(exc.code), payload_out


def _run_sweep_now() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with procrastinate_app.open_async():
            return await sweep(int(datetime.now(tz=UTC).timestamp()))

    return asyncio.run(_run())


def _seed_incident_ids(n: int) -> list[str]:
    ds1 = dataset_by_code("DS-1")
    ds1.build()
    ids = incident_ids_from_identity_set(
        {
            (str(row["id"]), None if row.get("thread_id") is None else str(row.get("thread_id")))
            for row in ds1.truth()
        }
    )
    return ids[:n]


def _job_rows(request_id: str) -> list[dict[str, Any]]:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text, page_no, status, attempts, owner, raw_uri,
                       raw_written_at, loaded_at, lease_expires_at, last_error
                FROM collector_job
                WHERE request_id = %s::uuid
                ORDER BY page_no
                """,
                (request_id,),
            )
            rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "job_id": str(row[0]),
                "page_no": int(row[1]),
                "status": str(row[2]),
                "attempts": int(row[3]),
                "owner": row[4],
                "raw_uri": row[5],
                "raw_written_at": None if row[6] is None else row[6].isoformat(),
                "loaded_at": None if row[7] is None else row[7].isoformat(),
                "lease_expires_at": None if row[8] is None else row[8].isoformat(),
                "last_error": row[9],
            }
        )
    return out


def _first_job_id(request_id: str) -> str:
    rows = _job_rows(request_id)
    if not rows:
        raise AssertionError(f"no collector_job rows for request {request_id}")
    return str(rows[0]["job_id"])


def _set_job_mutation(job_id: str, sql_set: str, params: tuple[Any, ...]) -> None:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE collector_job
                SET {sql_set},
                    updated_at = now()
                WHERE job_id = %s::uuid
                """,
                (*params, job_id),
            )
        conn.commit()


def _wait_until(predicate, timeout_s: int, poll_s: float = 2.0) -> tuple[bool, float]:
    start = time.monotonic()
    while True:
        if predicate():
            return True, time.monotonic() - start
        if time.monotonic() - start >= timeout_s:
            return False, time.monotonic() - start
        time.sleep(poll_s)


def test_e1_worker_killed_mid_fetch_recovery() -> None:
    reset_collector_state()
    request_id = post_collect("sentinel", {"incident_ids": _seed_incident_ids(100)})
    job_id = _first_job_id(request_id)
    # Simulate worker death mid-fetch by expiring an in-progress lease.
    _set_job_mutation(
        job_id,
        "status = 'in_progress', attempts = 1, lease_expires_at = now() - interval '1 minute', last_error = 'E-1 simulated mid-fetch kill'",
        (),
    )
    sweep_result = _run_sweep_now()
    ok, recovery_latency_s = _wait_until(
        lambda: _api_get_json(f"/v1/requests/{request_id}/counts").get("counts", {}).get("done", 0) >= 2,
        timeout_s=300,
    )
    write_evidence(
        "E-1",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"sweep_result={json.dumps(sweep_result, sort_keys=True)}",
            f"recovery_latency_s={recovery_latency_s}",
            f"final_rows={json.dumps(_job_rows(request_id), sort_keys=True)}",
        ],
    )
    assert ok, "E-1 recovery to done pages not observed"


def test_e2_killed_after_raw_write_before_load() -> None:
    reset_collector_state()
    request_id = post_collect("sentinel", {"incident_ids": _seed_incident_ids(50)})
    job_id = _first_job_id(request_id)
    _set_job_mutation(
        job_id,
        "status = 'in_progress', raw_uri = 'gs://forced/e2.json', raw_written_at = now() - interval '20 minutes', loaded_at = NULL, lease_expires_at = now() - interval '1 minute', last_error = 'E-2 simulated post-raw kill'",
        (),
    )
    before = _api_get_json("/v1/reconcile?minutes=0")
    sweep_result = _run_sweep_now()
    ok, recovery_latency_s = _wait_until(
        lambda: _api_get_json("/v1/reconcile?minutes=0").get("unloaded", 1) == 0,
        timeout_s=300,
    )
    after = _api_get_json("/v1/reconcile?minutes=0")
    write_evidence(
        "E-2",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"reconcile_before={json.dumps(before, sort_keys=True)}",
            f"sweep_result={json.dumps(sweep_result, sort_keys=True)}",
            f"recovery_latency_s={recovery_latency_s}",
            f"reconcile_after={json.dumps(after, sort_keys=True)}",
        ],
    )
    assert before.get("unloaded", 0) >= 1
    assert ok


def test_e3_killed_after_bq_insert_before_mark_done() -> None:
    reset_collector_state()
    request_id = post_collect("sentinel", {"incident_ids": _seed_incident_ids(50)})
    wait_request_terminal(request_id, timeout_s=300)
    job_id = _first_job_id(request_id)
    _set_job_mutation(
        job_id,
        "status = 'in_progress', loaded_at = NULL, lease_expires_at = now() - interval '1 minute', last_error = 'E-3 simulated post-bq pre-done kill'",
        (),
    )
    sweep_result = _run_sweep_now()
    ok, recovery_latency_s = _wait_until(
        lambda: _job_rows(request_id)[0]["status"] == "done",
        timeout_s=300,
    )
    write_evidence(
        "E-3",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"sweep_result={json.dumps(sweep_result, sort_keys=True)}",
            f"recovery_latency_s={recovery_latency_s}",
            f"final_row={json.dumps(_job_rows(request_id)[0], sort_keys=True)}",
        ],
    )
    assert ok


def test_e4_source_down_then_recovery_latency() -> None:
    reset_collector_state()
    ids = _seed_incident_ids(50)
    for ident in ids[:5]:
        _mock_call("POST", f"/admin/fault/{ident}")
    request_id = post_collect("sentinel", {"incident_ids": ids})
    ok_retry, t_retry = _wait_until(
        lambda: _job_rows(request_id)[0]["attempts"] >= 2,
        timeout_s=180,
    )
    _mock_call("DELETE", "/admin/fault")
    ok_done, recovery_latency_s = _wait_until(
        lambda: _job_rows(request_id)[0]["status"] == "done",
        timeout_s=300,
    )
    write_evidence(
        "E-4",
        [
            f"request_id={request_id}",
            f"retry_observed={ok_retry}",
            f"time_to_retry_s={t_retry}",
            f"recovery_latency_s={recovery_latency_s}",
            f"final_row={json.dumps(_job_rows(request_id)[0], sort_keys=True)}",
        ],
    )
    assert ok_retry
    assert ok_done


def test_e5_source_slow_against_short_lease_simulation() -> None:
    reset_collector_state()
    request_id = post_collect("sentinel", {"incident_ids": _seed_incident_ids(200)})
    rows = _job_rows(request_id)
    # Force one active page to look lease-expired while request continues.
    _set_job_mutation(
        str(rows[0]["job_id"]),
        "status = 'in_progress', lease_expires_at = now() - interval '1 minute', last_error='E-5 simulated short lease timeout'",
        (),
    )
    sweep_result = _run_sweep_now()
    terminal = wait_request_terminal(request_id, timeout_s=600)
    write_evidence(
        "E-5",
        [
            f"request_id={request_id}",
            f"sweep_result={json.dumps(sweep_result, sort_keys=True)}",
            f"terminal={terminal}",
        ],
    )
    assert terminal.failed == 0
    assert terminal.dead == 0


def test_e6_garbage_payloads() -> None:
    reset_collector_state()
    cases = [
        {},
        {"incident_ids": None},
        {"incident_ids": [None, {"x": 1}]},
        {"incident_ids": "not-a-list"},
        {"order_item_ids": ["not-num"]},
    ]
    lines: list[str] = []
    for idx, case in enumerate(cases, start=1):
        status, payload = post_collect_detailed("sentinel", case)
        lines.append(f"case={idx} status={status} payload={payload}")
        assert status < 500
    write_evidence("E-6", lines)


def test_e7_source_returning_unrequested_records() -> None:
    reset_collector_state()
    requested = _seed_incident_ids(20)
    request_id = post_collect("sentinel", {"incident_ids": requested})
    wait_request_terminal(request_id, timeout_s=300)
    observed = bq_identity_set(request_id)
    observed_ids = {incident_id for incident_id, _ in observed}
    unrequested = sorted(observed_ids - set(requested))
    write_evidence(
        "E-7",
        [
            f"request_id={request_id}",
            f"requested_count={len(requested)}",
            f"observed_ids_count={len(observed_ids)}",
            f"unrequested_count={len(unrequested)}",
            f"unrequested_first_3={unrequested[:3]}",
            f"unrequested_last_3={unrequested[-3:] if unrequested else []}",
        ],
    )
    assert len(unrequested) == 0


def test_e8_database_down_blocked() -> None:
    write_evidence(
        "E-8",
        [
            "BLOCKED: database-down fault injection not performed; shared postgres for active acceptance stack cannot be safely stopped in this run.",
        ],
    )
    pytest.skip("BLOCKED: cannot safely take shared postgres down in this environment")


def test_e9_sink_down_blocked() -> None:
    write_evidence(
        "E-9",
        [
            "BLOCKED: sink-down fault injection not performed; shared live GCS/BigQuery credentials are active and not isolated per-test in this run.",
        ],
    )
    pytest.skip("BLOCKED: cannot safely isolate sink outage in shared environment")


def test_e10_permanent_failure_to_dead_with_elapsed_floor() -> None:
    reset_collector_state()
    ids = _seed_incident_ids(50)
    doomed_id = ids[0]
    _mock_call("DELETE", "/admin/fault")
    _mock_call("DELETE", "/admin/stats")
    _mock_call("POST", f"/admin/fault/{doomed_id}")
    request_id = post_collect("sentinel", {"incident_ids": [doomed_id]})
    job_id = _first_job_id(request_id)
    cap_s = 30 * 60
    start_calls = int(_mock_call("GET", "/admin/stats")[1].get("requests", 0))
    start = time.monotonic()
    time_to_dead_letter_s: float | None = None
    sweeps: list[dict[str, Any]] = []
    while True:
        elapsed = time.monotonic() - start
        rows = _job_rows(request_id)
        row = rows[0]
        # Let real retries happen first; after 60s, force lease expiry path to continue progression.
        if elapsed > 60 and row["status"] == "in_progress":
            _set_job_mutation(
                job_id,
                "lease_expires_at = now() - interval '1 minute'",
                (),
            )
        sweeps.append(_run_sweep_now())
        dead = _api_get_json("/v1/dead-letter")
        if any(str(r.get("job_id")) == job_id for r in dead.get("rows", [])):
            time_to_dead_letter_s = time.monotonic() - start
            break
        if elapsed > cap_s:
            break
        time.sleep(5)
    end_calls = int(_mock_call("GET", "/admin/stats")[1].get("requests", 0))
    elapsed_s = time.monotonic() - start
    calls_burned = end_calls - start_calls
    print("E-10 recovery_latency_s=N/A")
    print(f"E-10 time_to_dead_letter_s={time_to_dead_letter_s}")
    print(f"E-10 calls_burned={calls_burned}")
    print(f"E-10 elapsed_s={elapsed_s:.3f}")
    write_evidence(
        "E-10",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"time_to_dead_letter_s={time_to_dead_letter_s}",
            f"calls_burned={calls_burned}",
            f"elapsed_s={elapsed_s:.6f}",
            f"final_row={json.dumps(_job_rows(request_id)[0], sort_keys=True)}",
            f"sweeps={json.dumps(sweeps, sort_keys=True)}",
        ],
    )
    _mock_call("DELETE", "/admin/fault")
    assert elapsed_s <= cap_s, f"E-10 exceeded cap: {elapsed_s:.3f}s > {cap_s}s"
    assert elapsed_s > 60.0, f"E-10 elapsed floor not met: {elapsed_s:.3f}s <= 60s"
    assert calls_burned > 0
    assert time_to_dead_letter_s is not None


def test_e11_240s_hold_sampling_every_15s_with_elapsed_assertion() -> None:
    reset_collector_state()
    request_id = post_collect("sentinel", {"incident_ids": _seed_incident_ids(50)})
    job_id = _first_job_id(request_id)
    _set_job_mutation(
        job_id,
        "status = 'in_progress', attempts = 1, lease_expires_at = now() - interval '1 minute', last_error = 'E-11 forced recovery sample'",
        (),
    )
    hold_s = 240
    interval_s = 15
    start = time.monotonic()
    recovery_latency_s: float | None = None
    samples: list[dict[str, Any]] = []
    while True:
        elapsed_now = time.monotonic() - start
        detail = _api_get_json("/v1/health/detail")
        row = _job_rows(request_id)[0]
        sweep_result = _run_sweep_now()
        samples.append(
            {
                "elapsed_s": round(elapsed_now, 3),
                "job_status": row["status"],
                "attempts": row["attempts"],
                "health_dead": detail.get("dead"),
                "health_unloaded": detail.get("unloaded"),
                "sweep": sweep_result,
            }
        )
        if recovery_latency_s is None and row["status"] == "done":
            recovery_latency_s = elapsed_now
        if elapsed_now >= hold_s:
            break
        time.sleep(interval_s)
    elapsed_s = time.monotonic() - start
    print(f"E-11 recovery_latency_s={recovery_latency_s}")
    print("E-11 time_to_dead_letter_s=N/A")
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
    assert recovery_latency_s is not None


def test_e12_compound_failure_source_down_plus_lease_expiry() -> None:
    reset_collector_state()
    ids = _seed_incident_ids(50)
    for ident in ids[:3]:
        _mock_call("POST", f"/admin/fault/{ident}")
    request_id = post_collect("sentinel", {"incident_ids": ids})
    job_id = _first_job_id(request_id)
    _set_job_mutation(
        job_id,
        "status='in_progress', lease_expires_at = now() - interval '1 minute', last_error='E-12 compound failure seed'",
        (),
    )
    sweep_one = _run_sweep_now()
    _mock_call("DELETE", "/admin/fault")
    ok, recovery_latency_s = _wait_until(
        lambda: _api_get_json(f"/v1/requests/{request_id}/counts").get("counts", {}).get("done", 0) >= 1,
        timeout_s=600,
    )
    terminal = wait_request_terminal(request_id, timeout_s=900)
    write_evidence(
        "E-12",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"sweep_one={json.dumps(sweep_one, sort_keys=True)}",
            f"recovery_latency_s={recovery_latency_s}",
            f"terminal={terminal}",
            f"final_row={json.dumps(_job_rows(request_id)[0], sort_keys=True)}",
        ],
    )
    assert ok
    assert terminal.failed == 0
