from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

API_URL = "https://collector-api-mfo5qzthxa-el.a.run.app"
MOCK_URL = "https://mock-sentinel-mfo5qzthxa-el.a.run.app"
PROJECT = "clariversev1"
REGION = "asia-south1"
SENTINEL_JOB = "col-sentinel"
DISCOVERY_JOB = "col-sentinel-discovery"
EVIDENCE_DIR = Path("/workspace/tests/deployed/evidence")


def _run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def _run_json(cmd: list[str]) -> Any:
    out = _run(cmd)
    return json.loads(out) if out else None


def _id_token(audience: str) -> str:
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


def _list_running_executions(job: str) -> list[str]:
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
            "--limit=30",
            "--format=json",
        ]
    ) or []
    running: list[str] = []
    for row in rows:
        completed = [
            c for c in row.get("status", {}).get("conditions", []) if c.get("type") == "Completed"
        ]
        status = completed[0].get("status") if completed else None
        name = row.get("metadata", {}).get("name")
        if name and status != "True":
            running.append(str(name))
    return running


def _cancel_running(job: str) -> list[str]:
    cancelled: list[str] = []
    for execution in _list_running_executions(job):
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


def _start_execution(job: str, tasks: int) -> str:
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


def _collect(api_token: str, source: str, query_spec: dict[str, Any]) -> tuple[str, int]:
    resp = _http_json("POST", API_URL + "/v1/collect", api_token, {"source": source, "query_spec": query_spec})
    rid = str(resp["request_id"])
    total_pages = int(resp.get("total_pages") or 0)
    return rid, total_pages


def _wait_terminal(api_token: str, request_id: str, total_pages: int, timeout_s: int = 3600) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = _http_json("GET", API_URL + f"/v1/requests/{request_id}/counts", api_token)
        counts = last.get("counts", {})
        done = int(counts.get("done", 0))
        failed = int(counts.get("failed", 0))
        dead = int(counts.get("dead", 0))
        if done + failed + dead >= total_pages:
            return last
        time.sleep(2)
    raise AssertionError(f"request {request_id} not terminal in {timeout_s}s; last={last}")


def _mock_discover_ids(mock_token: str, need: int) -> list[str]:
    out: list[str] = []
    cursor: str | None = None
    while len(out) < need:
        payload: dict[str, Any] = {
            "updated_from": "2026-08-20T00:00:00Z",
            "updated_to": "2026-08-27T00:00:00Z",
            "limit": 1000,
        }
        if cursor:
            payload["cursor"] = cursor
        page = _http_json("POST", MOCK_URL + "/v1/incidents/discover", mock_token, payload)
        out.extend(str(x) for x in page.get("incident_ids", []))
        cursor = page.get("next_cursor")
        if not cursor:
            break
    if len(out) < need:
        raise AssertionError(f"mock discover returned {len(out)} ids, need {need}")
    return out[:need]


def _reset_mock_stats(mock_token: str) -> None:
    req = urllib.request.Request(MOCK_URL + "/admin/stats", method="DELETE", headers={"Authorization": f"Bearer {mock_token}"})
    with urllib.request.urlopen(req, timeout=60):
        return


def _read_mock_stats(mock_token: str) -> int:
    req = urllib.request.Request(MOCK_URL + "/admin/stats", method="GET", headers={"Authorization": f"Bearer {mock_token}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return int(payload.get("requests") or 0)


def _measure_calls_per_second(
    api_token: str,
    mock_token: str,
    *,
    sentinel_tasks: int,
    discovery_tasks: int,
    window_s: int,
) -> dict[str, Any]:
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)

    sentinel_execution: str | None = None
    discovery_execution: str | None = None
    if sentinel_tasks > 0:
        sentinel_execution = _start_execution(SENTINEL_JOB, sentinel_tasks)
    if discovery_tasks > 0:
        discovery_execution = _start_execution(DISCOVERY_JOB, discovery_tasks)

    # Give worker containers time to boot and heartbeat.
    time.sleep(12)

    if sentinel_tasks > 0:
        ids = _mock_discover_ids(mock_token, need=sentinel_tasks * 5000)
        _collect(api_token, "sentinel", {"incident_ids": ids})
    if discovery_tasks > 0:
        _collect(
            api_token,
            "sentinel_discovery",
            {
                "updated_from": "2026-08-20T00:00:00Z",
                "updated_to": "2026-08-27T00:00:00Z",
            },
        )

    _reset_mock_stats(mock_token)
    started = time.monotonic()
    time.sleep(window_s)
    ended = time.monotonic()
    requests = _read_mock_stats(mock_token)
    elapsed = ended - started
    cps = requests / elapsed

    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)

    return {
        "sentinel_tasks": sentinel_tasks,
        "discovery_tasks": discovery_tasks,
        "window_s": window_s,
        "elapsed_s": elapsed,
        "source_requests": requests,
        "calls_per_second": cps,
        "sentinel_execution": sentinel_execution,
        "discovery_execution": discovery_execution,
    }


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    return {"api": _id_token(API_URL), "mock": _id_token(MOCK_URL)}


@pytest.fixture(scope="session")
def collector_postgres_version() -> str:
    return _run(["bash", "-lc", "set -a; source .env; set +a; psql \"$COLLECTOR_DSN\" -tAc \"select version();\""])


@pytest.fixture(scope="session")
def c1_rates(tokens: dict[str, str]) -> dict[int, dict[str, Any]]:
    measurements: dict[int, dict[str, Any]] = {}
    for tasks in (1, 2, 3):
        measurements[tasks] = _measure_calls_per_second(
            tokens["api"],
            tokens["mock"],
            sentinel_tasks=tasks,
            discovery_tasks=0,
            window_s=60,
        )
    return measurements


def test_c1_calls_per_second_floor(c1_rates: dict[int, dict[str, Any]], collector_postgres_version: str) -> None:
    # Protocol rule: this test must enforce a 60-second floor itself.
    for tasks, row in c1_rates.items():
        assert row["elapsed_s"] >= 60.0, f"C-1 floor violated for {tasks} tasks: {row['elapsed_s']:.3f}s"
        assert row["calls_per_second"] > 0.0, f"non-positive calls/sec for {tasks} tasks"
    _write_evidence(
        "C-1",
        [
            f"postgres_version={collector_postgres_version}",
            f"tasks=1 calls_per_second={c1_rates[1]['calls_per_second']:.6f} requests={c1_rates[1]['source_requests']} elapsed_s={c1_rates[1]['elapsed_s']:.3f}",
            f"tasks=2 calls_per_second={c1_rates[2]['calls_per_second']:.6f} requests={c1_rates[2]['source_requests']} elapsed_s={c1_rates[2]['elapsed_s']:.3f}",
            f"tasks=3 calls_per_second={c1_rates[3]['calls_per_second']:.6f} requests={c1_rates[3]['source_requests']} elapsed_s={c1_rates[3]['elapsed_s']:.3f}",
        ],
    )


def test_c2_global_ceiling(c1_rates: dict[int, dict[str, Any]]) -> None:
    cps1 = float(c1_rates[1]["calls_per_second"])
    cps2 = float(c1_rates[2]["calls_per_second"])
    cps3 = float(c1_rates[3]["calls_per_second"])
    ratio_2x = cps2 / cps1 if cps1 else 0.0
    ratio_3x = cps3 / cps1 if cps1 else 0.0
    global_ceiling_exists = ratio_2x < 1.6 or ratio_3x < 2.5
    _write_evidence(
        "C-2",
        [
            f"calls_per_second_tasks_1={cps1:.6f}",
            f"calls_per_second_tasks_2={cps2:.6f}",
            f"calls_per_second_tasks_3={cps3:.6f}",
            f"ratio_2x={ratio_2x:.6f}",
            f"ratio_3x={ratio_3x:.6f}",
            f"global_ceiling_exists={global_ceiling_exists}",
        ],
    )
    assert cps3 > cps1, "throughput should not drop when raising tasks from 1 to 3"


def test_c3_rolling_redeploy_peak(tokens: dict[str, str], c1_rates: dict[int, dict[str, Any]]) -> None:
    _cancel_running(SENTINEL_JOB)
    base_execution = _start_execution(SENTINEL_JOB, 3)
    time.sleep(12)

    ids = _mock_discover_ids(tokens["mock"], need=20000)
    _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    _reset_mock_stats(tokens["mock"])

    # Overlap old+new execution as a rolling redeploy analogue.
    time.sleep(20)
    overlap_execution = _start_execution(SENTINEL_JOB, 3)

    samples: list[tuple[float, int]] = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        now = time.monotonic()
        samples.append((now, _read_mock_stats(tokens["mock"])))
        time.sleep(5)

    peak_cps = 0.0
    for i in range(1, len(samples)):
        dt_s = samples[i][0] - samples[i - 1][0]
        dr = samples[i][1] - samples[i - 1][1]
        if dt_s > 0:
            peak_cps = max(peak_cps, dr / dt_s)

    baseline_cps = float(c1_rates[3]["calls_per_second"])
    _write_evidence(
        "C-3",
        [
            f"base_execution={base_execution}",
            f"overlap_execution={overlap_execution}",
            f"baseline_calls_per_second_3_tasks={baseline_cps:.6f}",
            f"rolling_redeploy_peak_calls_per_second={peak_cps:.6f}",
            f"rolling_redeploy_peak_multiplier={peak_cps / baseline_cps if baseline_cps else 0.0:.6f}",
        ],
    )
    _cancel_running(SENTINEL_JOB)
    assert peak_cps > 0.0


def test_c4_discovery_and_enrichment_independent_rates(tokens: dict[str, str]) -> None:
    enrich = _measure_calls_per_second(
        tokens["api"],
        tokens["mock"],
        sentinel_tasks=3,
        discovery_tasks=0,
        window_s=60,
    )
    discover = _measure_calls_per_second(
        tokens["api"],
        tokens["mock"],
        sentinel_tasks=0,
        discovery_tasks=1,
        window_s=60,
    )
    concurrent = _measure_calls_per_second(
        tokens["api"],
        tokens["mock"],
        sentinel_tasks=3,
        discovery_tasks=1,
        window_s=60,
    )

    sum_alone = float(enrich["calls_per_second"]) + float(discover["calls_per_second"])
    combined = float(concurrent["calls_per_second"])
    independent_rates_hold = combined >= (sum_alone * 0.85)
    _write_evidence(
        "C-4",
        [
            f"enrichment_alone_calls_per_second={enrich['calls_per_second']:.6f}",
            f"discovery_alone_calls_per_second={discover['calls_per_second']:.6f}",
            f"concurrent_calls_per_second={concurrent['calls_per_second']:.6f}",
            f"sum_of_alone_calls_per_second={sum_alone:.6f}",
            f"concurrent_over_sum_ratio={combined / sum_alone if sum_alone else 0.0:.6f}",
            f"independent_rates_hold={independent_rates_hold}",
        ],
    )
    assert enrich["calls_per_second"] > 0.0 and discover["calls_per_second"] > 0.0

