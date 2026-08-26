from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from tests.acceptance.helpers import (
    dataset_by_code,
    incident_ids_from_identity_set,
    post_collect,
    reset_collector_state,
    write_evidence,
)


def _mock_request(method: str, path: str) -> dict[str, Any]:
    req = urllib.request.Request(f"http://127.0.0.1:8081{path}", method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _mock_stats_reset() -> None:
    _mock_request("DELETE", "/admin/stats")


def _mock_stats_count() -> int:
    return int(_mock_request("GET", "/admin/stats").get("requests", 0))


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("/workspace"))
    env["PROCRASTINATE_APP"] = "collector.app.app"
    return env


def _start_extra_workers(n: int) -> list[subprocess.Popen[bytes]]:
    procs: list[subprocess.Popen[bytes]] = []
    if n <= 0:
        return procs
    for _ in range(n):
        proc = subprocess.Popen(
            [
                "/workspace/.venv/bin/python",
                "-m",
                "procrastinate",
                "worker",
                "-q",
                "sentinel",
                "-c",
                "1",
                "--delete-jobs",
                "never",
            ],
            cwd="/workspace",
            env=_worker_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
    time.sleep(2)
    return procs


def _stop_extra_workers(procs: list[subprocess.Popen[bytes]]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _ds2_ids() -> list[str]:
    ds2 = dataset_by_code("DS-2")
    ds2.build()
    truth_rows = ds2.truth()
    return sorted({str(row["id"]) for row in truth_rows})


def _measure_calls_per_second(*, target_workers: int, ids_needed: int, measure_seconds: int) -> dict[str, Any]:
    reset_collector_state()
    all_ids = _ds2_ids()
    incident_ids = all_ids[:ids_needed]
    extra = max(target_workers - 1, 0)
    procs = _start_extra_workers(extra)
    try:
        _mock_stats_reset()
        start_count = _mock_stats_count()
        request_id = post_collect("sentinel", {"incident_ids": incident_ids})
        started = time.monotonic()
        time.sleep(measure_seconds)
        elapsed = time.monotonic() - started
        end_count = _mock_stats_count()
        cps = (end_count - start_count) / elapsed if elapsed > 0 else 0.0
        # Intentionally stop after the fixed measurement window.
        with subprocess.Popen(
            ["/workspace/.venv/bin/python", "-c", "import os,psycopg; "
             "dsn=os.environ['COLLECTOR_DSN']; "
             "conn=psycopg.connect(dsn); cur=conn.cursor(); "
             f"cur.execute(\"SELECT total_pages FROM collector_request WHERE request_id = '{request_id}'::uuid\"); "
             "tp=cur.fetchone()[0]; "
             f"cur.execute(\"SELECT count(*) FILTER (WHERE status='done'), count(*) FILTER (WHERE status='failed'), count(*) FILTER (WHERE status='dead') FROM collector_job WHERE request_id = '{request_id}'::uuid\"); "
             "d,f,dd=cur.fetchone(); "
             "print(f'{tp},{d},{f},{dd}'); conn.close()"],
            cwd="/workspace",
            env=_worker_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as proc:
            out, _ = proc.communicate(timeout=30)
        total_pages, done_pages, failed_pages, dead_pages = [int(x) for x in out.decode("utf-8").strip().split(",")]
        return {
            "request_id": request_id,
            "workers_target": target_workers,
            "workers_extra_started": extra,
            "measure_seconds": measure_seconds,
            "elapsed_s": elapsed,
            "start_count": start_count,
            "end_count": end_count,
            "delta_calls": end_count - start_count,
            "calls_per_second": cps,
            "ids_used": len(incident_ids),
            "total_pages": total_pages,
            "done_pages": done_pages,
            "failed_pages": failed_pages,
            "dead_pages": dead_pages,
        }
    finally:
        _stop_extra_workers(procs)


def test_d1_60_second_floor_and_rate_capture() -> None:
    result = _measure_calls_per_second(target_workers=1, ids_needed=4000, measure_seconds=60)
    print(f"D-1 calls_per_second_workers_1={result['calls_per_second']:.6f}")
    print(f"D-1 elapsed_s={result['elapsed_s']:.6f}")
    write_evidence(
        "D-1",
        [
            json.dumps(result, sort_keys=True),
        ],
    )
    assert result["elapsed_s"] >= 60.0, f"D-1 under-ran floor: {result['elapsed_s']:.3f}s < 60s"
    assert result["calls_per_second"] > 0.0


def test_d2_rate_capture_three_workers() -> None:
    result = _measure_calls_per_second(target_workers=3, ids_needed=12000, measure_seconds=60)
    print(f"D-2 calls_per_second_workers_3={result['calls_per_second']:.6f}")
    write_evidence("D-2", [json.dumps(result, sort_keys=True)])
    assert result["calls_per_second"] > 0.0


def test_d3_rate_capture_six_workers() -> None:
    result = _measure_calls_per_second(target_workers=6, ids_needed=21000, measure_seconds=60)
    print(f"D-3 calls_per_second_workers_6={result['calls_per_second']:.6f}")
    write_evidence("D-3", [json.dumps(result, sort_keys=True)])
    assert result["calls_per_second"] > 0.0
