from __future__ import annotations

import json
import math
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from google.cloud.sql.connector import Connector

API_URL = "https://collector-api-mfo5qzthxa-el.a.run.app"
MOCK_URL = "https://mock-sentinel-mfo5qzthxa-el.a.run.app"
PROJECT = "clariversev1"
REGION = "asia-south1"
SENTINEL_JOB = "col-sentinel"
DISCOVERY_JOB = "col-sentinel-discovery"
MAINT_JOB = "col-maintenance"
EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"

# Protocol reference population from prior acceptance context (DS-2).
DS2_POPULATION = 299190
BATCH_CAP = 50


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def _run_json(cmd: list[str]) -> Any:
    out = _run(cmd)
    return json.loads(out) if out else None


def _token(audience: str) -> str:
    return _run(["gcloud", "auth", "print-identity-token", "--audiences=" + audience])


def _http_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _write_evidence(test_id: str, lines: list[str]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{test_id}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _list_running(job: str) -> list[str]:
    rows = _run_json(
        [
            "gcloud",
            "run",
            "jobs",
            "executions",
            "list",
            f"--job={job}",
            f"--region={REGION}",
            f"--project={PROJECT}",
            "--limit=40",
            "--format=json",
        ]
    ) or []
    out: list[str] = []
    for row in rows:
        done_status = None
        for cond in row.get("status", {}).get("conditions", []):
            if cond.get("type") == "Completed":
                done_status = cond.get("status")
                break
        name = row.get("metadata", {}).get("name")
        if name and done_status != "True":
            out.append(str(name))
    return out


def _cancel_running(job: str) -> list[str]:
    cancelled: list[str] = []
    for execution in _list_running(job):
        subprocess.run(
            [
                "gcloud",
                "run",
                "jobs",
                "executions",
                "cancel",
                execution,
                f"--region={REGION}",
                f"--project={PROJECT}",
                "--quiet",
            ],
            check=False,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cancelled.append(execution)
    time.sleep(5)
    return cancelled


def _start(job: str, tasks: int) -> str:
    return _run(
        [
            "gcloud",
            "run",
            "jobs",
            "execute",
            job,
            f"--region={REGION}",
            f"--project={PROJECT}",
            f"--tasks={tasks}",
            "--format=value(metadata.name)",
        ]
    )


def _restore_baseline() -> dict[str, str]:
    return {
        "sentinel": _start(SENTINEL_JOB, 3),
        "discovery": _start(DISCOVERY_JOB, 1),
        "maintenance": _start(MAINT_JOB, 1),
    }


def _collect(api_token: str, source: str, query_spec: dict[str, Any]) -> tuple[str, int]:
    resp = _http_json("POST", API_URL + "/v1/collect", api_token, {"source": source, "query_spec": query_spec})
    return str(resp["request_id"]), int(resp.get("total_pages") or 0)


def _counts(api_token: str, request_id: str) -> dict[str, Any]:
    return _http_json("GET", API_URL + f"/v1/requests/{request_id}/counts", api_token)


def _wait_terminal(api_token: str, request_id: str, total_pages: int, timeout_s: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _counts(api_token, request_id)
        c = last.get("counts", {})
        done = int(c.get("done", 0))
        failed = int(c.get("failed", 0))
        dead = int(c.get("dead", 0))
        if done + failed + dead >= total_pages:
            return last
        time.sleep(2)
    raise AssertionError(f"request {request_id} not terminal in {timeout_s}s; last={last}")


def _mock_stats_get(mock_token: str) -> int:
    def _call(token: str) -> int:
        req = urllib.request.Request(
            MOCK_URL + "/admin/stats",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return int(payload.get("requests") or 0)

    try:
        return _call(mock_token)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        refreshed = _token(MOCK_URL)
        return _call(refreshed)


def _mock_discover_ids(mock_token: str, need: int | None) -> list[str]:
    out: list[str] = []
    cursor: str | None = None
    while True:
        payload: dict[str, Any] = {
            "updated_from": "2026-08-20T00:00:00Z",
            "updated_to": "2026-08-27T00:00:00Z",
            "limit": 1000,
        }
        if cursor:
            payload["cursor"] = cursor
        page = _http_json("POST", MOCK_URL + "/v1/incidents/discover", mock_token, payload)
        out.extend(str(x) for x in page.get("incident_ids", []))
        if need is not None and len(out) >= need:
            return out[:need]
        cursor = page.get("next_cursor")
        if not cursor:
            return out


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    return {"api": _token(API_URL), "mock": _token(MOCK_URL)}


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup() -> Any:
    # F-4 is intentionally excluded in this module.
    # No calls to collector admin delete endpoints are made anywhere in this file.
    yield
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    _restore_baseline()


def test_throughput_floor_60s(tokens: dict[str, str]) -> None:
    _cancel_running(SENTINEL_JOB)
    exec_name = _start(SENTINEL_JOB, 3)
    time.sleep(12)
    ids = _mock_discover_ids(tokens["mock"], need=20000)
    _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    before = _mock_stats_get(tokens["mock"])
    t0 = time.monotonic()
    time.sleep(60)
    elapsed = time.monotonic() - t0
    after = _mock_stats_get(tokens["mock"])
    assert elapsed >= 60.0, f"60s floor violated: {elapsed:.3f}s"
    reqs = max(after - before, 0)
    cps = reqs / elapsed
    ppm_per_worker = (cps * 60.0) / 3.0
    _write_evidence(
        "throughput_floor_60s",
        [
            f"execution={exec_name}",
            f"window_elapsed_s={elapsed:.3f}",
            f"source_requests_before={before}",
            f"source_requests_after={after}",
            f"source_requests_delta={reqs}",
            f"calls_per_second={cps:.6f}",
            f"pages_per_minute_per_worker={ppm_per_worker:.6f}",
            "scenario_status=measurement_only",
        ],
    )
    assert reqs > 0


def test_single_page_latency_during_backlog(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=30000)
    sweep_rid, sweep_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    probe_ids = ids[:50]
    t0 = time.monotonic()
    probe_rid, probe_pages = _collect(tokens["api"], "sentinel", {"incident_ids": probe_ids})
    probe_terminal = _wait_terminal(tokens["api"], probe_rid, probe_pages, timeout_s=3600)
    probe_elapsed = time.monotonic() - t0
    sweep_terminal = _wait_terminal(tokens["api"], sweep_rid, sweep_pages, timeout_s=7200)
    _write_evidence(
        "single_page_latency_during_backlog",
        [
            f"sweep_request_id={sweep_rid}",
            f"sweep_total_pages={sweep_pages}",
            f"sweep_counts={sweep_terminal.get('counts', {})}",
            f"probe_request_id={probe_rid}",
            f"probe_total_pages={probe_pages}",
            f"probe_counts={probe_terminal.get('counts', {})}",
            f"probe_elapsed_s={probe_elapsed:.3f}",
            f"scenario_status={'recovered_with_delay' if probe_elapsed > 120.0 else 'recovered'}",
        ],
    )
    assert int(probe_terminal.get("counts", {}).get("done", 0)) == probe_pages


def test_run_to_conclusion_and_30m_capacity(tokens: dict[str, str]) -> None:
    all_ids = list(dict.fromkeys(_mock_discover_ids(tokens["mock"], need=None)))
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": all_ids})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=7200)
    elapsed = time.monotonic() - t0
    done = int(terminal.get("counts", {}).get("done", 0))
    elapsed_min = elapsed / 60.0
    pages_per_min_total = done / elapsed_min if elapsed_min > 0 else 0.0
    pages_per_min_per_worker = pages_per_min_total / 3.0
    pop_ceiling_30m = int(math.floor(pages_per_min_per_worker * 3.0 * 30.0 * BATCH_CAP))
    margin_vs_ds2 = pop_ceiling_30m - DS2_POPULATION
    status = "capacity_ok" if margin_vs_ds2 >= 0 else "capacity_short"
    _write_evidence(
        "run_to_conclusion_and_30m_capacity",
        [
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"terminal_counts={terminal.get('counts', {})}",
            f"elapsed_s={elapsed:.3f}",
            f"elapsed_min={elapsed_min:.6f}",
            f"pages_per_minute_total={pages_per_min_total:.6f}",
            f"pages_per_minute_per_worker={pages_per_min_per_worker:.6f}",
            f"derived_population_ceiling_30m={pop_ceiling_30m}",
            f"ds2_population={DS2_POPULATION}",
            f"margin_vs_ds2={margin_vs_ds2}",
            f"scenario_status={status}",
        ],
    )
    assert done == total_pages


def test_discovery_and_enrichment_parallel_rate(tokens: dict[str, str]) -> None:
    # Measure enrichment only
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    enrich_exec = _start(SENTINEL_JOB, 3)
    time.sleep(12)
    ids = _mock_discover_ids(tokens["mock"], need=12000)
    _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    e_before = _mock_stats_get(tokens["mock"])
    t0 = time.monotonic()
    time.sleep(60)
    e_elapsed = time.monotonic() - t0
    e_after = _mock_stats_get(tokens["mock"])
    enrich_cps = max(e_after - e_before, 0) / e_elapsed

    # Measure discovery only
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    discover_exec = _start(DISCOVERY_JOB, 1)
    time.sleep(10)
    _collect(
        tokens["api"],
        "sentinel_discovery",
        {"updated_from": "2026-08-20T00:00:00Z", "updated_to": "2026-08-27T00:00:00Z"},
    )
    d_before = _mock_stats_get(tokens["mock"])
    t1 = time.monotonic()
    time.sleep(60)
    d_elapsed = time.monotonic() - t1
    d_after = _mock_stats_get(tokens["mock"])
    discover_cps = max(d_after - d_before, 0) / d_elapsed

    # Measure concurrent
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    combo_exec_s = _start(SENTINEL_JOB, 3)
    combo_exec_d = _start(DISCOVERY_JOB, 1)
    time.sleep(12)
    ids2 = _mock_discover_ids(tokens["mock"], need=12000)
    _collect(tokens["api"], "sentinel", {"incident_ids": ids2})
    _collect(
        tokens["api"],
        "sentinel_discovery",
        {"updated_from": "2026-08-20T00:00:00Z", "updated_to": "2026-08-27T00:00:00Z"},
    )
    c_before = _mock_stats_get(tokens["mock"])
    t2 = time.monotonic()
    time.sleep(60)
    c_elapsed = time.monotonic() - t2
    c_after = _mock_stats_get(tokens["mock"])
    combined_cps = max(c_after - c_before, 0) / c_elapsed

    sum_alone = enrich_cps + discover_cps
    ratio = (combined_cps / sum_alone) if sum_alone > 0 else 0.0
    status = "recovered" if ratio >= 0.85 else "recovered_with_delay"
    _write_evidence(
        "discovery_and_enrichment_parallel_rate",
        [
            f"enrichment_execution={enrich_exec}",
            f"discovery_execution={discover_exec}",
            f"combo_execution_sentinel={combo_exec_s}",
            f"combo_execution_discovery={combo_exec_d}",
            f"enrichment_calls_per_second={enrich_cps:.6f}",
            f"discovery_calls_per_second={discover_cps:.6f}",
            f"combined_calls_per_second={combined_cps:.6f}",
            f"sum_alone_calls_per_second={sum_alone:.6f}",
            f"combined_over_sum_ratio={ratio:.6f}",
            f"scenario_status={status}",
        ],
    )
    assert enrich_cps > 0 and discover_cps > 0 and combined_cps > 0


def _worker_ids_live(prefix: str = "sentinel-task") -> set[str]:
    conn_name = os.environ.get("CONN")
    dbpw = os.environ.get("DBPW")
    if not conn_name or not dbpw:
        pytest.skip("CONN/DBPW required for F-3 identity check")
    connector = Connector()
    conn = connector.connect(
        conn_name, "pg8000", user="postgres", password=dbpw, db="collector"
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id
            FROM procrastinate_workers
            WHERE id LIKE %s
              AND now() - last_heartbeat < interval '90 seconds'
            """,
            (f"{prefix}%",),
        )
        return {str(r[0]) for r in cur.fetchall()}
    finally:
        conn.close()
        connector.close()


def test_f3_worker_identity_stability(tokens: dict[str, str]) -> None:
    """Protocol F-3: CLOUD_RUN_TASK_INDEX-derived identities survive restart.

    Cloud Run Jobs has no per-task cancel API in our tooling, so we cancel the
    whole execution (all three tasks) and re-execute with --tasks=3. Identity
    stability is still asserted per task index (sentinel-task0/1/2).
    Restart is performed by this test — unattended restart is F-1 / NOT IMPLEMENTED.
    """
    expected = {"sentinel-task0", "sentinel-task1", "sentinel-task2"}
    _cancel_running(SENTINEL_JOB)
    base_exec = _start(SENTINEL_JOB, 3)
    time.sleep(15)
    before_ids = _worker_ids_live()
    ids = _mock_discover_ids(tokens["mock"], need=500)
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    time.sleep(8)
    cancelled = _cancel_running(SENTINEL_JOB)
    mid = _http_json("GET", API_URL + f"/v1/requests/{rid}/counts", tokens["api"])
    time.sleep(5)
    during = _worker_ids_live()
    restart_exec = _start(SENTINEL_JOB, 3)
    deadline = time.monotonic() + 120.0
    after_ids: set[str] = set()
    while time.monotonic() < deadline:
        after_ids = _worker_ids_live()
        if expected <= after_ids:
            break
        time.sleep(3)
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=3600)
    done = int(terminal.get("counts", {}).get("done", 0))
    _write_evidence(
        "F-3",
        [
            f"base_execution={base_exec}",
            f"restart_execution={restart_exec}",
            f"cancelled_executions={cancelled}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"worker_ids_before={sorted(before_ids)}",
            f"worker_ids_during_gap={sorted(during)}",
            f"worker_ids_after={sorted(after_ids)}",
            f"expected_identities={sorted(expected)}",
            f"mid_counts={mid.get('counts', {})}",
            f"terminal_counts={terminal.get('counts', {})}",
            "note=restart performed by this test; unattended restart unmeasured (see F-1)",
        ],
    )
    assert expected <= after_ids, (
        f"expected task-index identities {expected} after restart, got {after_ids}"
    )
    assert done == total_pages

