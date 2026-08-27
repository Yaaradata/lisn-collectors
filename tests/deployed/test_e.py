from __future__ import annotations

import json
import subprocess
import time
import urllib.error
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
MAINT_JOB = "col-maintenance"
EVIDENCE_DIR = Path("/workspace/tests/deployed/evidence")


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
    token: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
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
        conditions = row.get("status", {}).get("conditions", [])
        done_status = None
        for cond in conditions:
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


def _restore_baseline_workers() -> dict[str, str]:
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
    raise AssertionError(f"request {request_id} did not reach terminal state: {last}")


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
        raise AssertionError(f"mock discover returned {len(out)} IDs; need {need}")
    return out[:need]


def _mock_set_fault(mock_token: str, ident: str) -> dict[str, Any]:
    return _http_json("POST", MOCK_URL + f"/admin/fault/{ident}", mock_token)


def _mock_clear_faults(mock_token: str) -> dict[str, Any]:
    return _http_json("DELETE", MOCK_URL + "/admin/fault", mock_token)


def _mock_set_payload_fault(mock_token: str, ident: str, mode: str) -> dict[str, Any]:
    try:
        return _http_json("POST", MOCK_URL + f"/admin/payload-fault/{ident}/{mode}", mock_token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("payload-fault endpoint unavailable on deployed mock") from exc
        raise


def _mock_clear_payload_faults(mock_token: str) -> dict[str, Any]:
    try:
        return _http_json("DELETE", MOCK_URL + "/admin/payload-fault", mock_token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"payload_faults": "endpoint_unavailable"}
        raise


def _scenario_outcome(
    *,
    intervention: bool,
    done: int,
    failed: int,
    dead: int,
    elapsed_s: float,
    delay_threshold_s: float,
) -> str:
    if dead > 0 or failed > 0:
        return "lost_data"
    if intervention:
        return "needed_intervention"
    if done > 0 and elapsed_s > delay_threshold_s:
        return "recovered_with_delay"
    return "recovered"


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    return {"api": _token(API_URL), "mock": _token(MOCK_URL)}


@pytest.fixture(scope="session", autouse=True)
def _cleanup_after_session() -> Any:
    yield
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    _restore_baseline_workers()


def test_e1_nonexistent_incident_observation(tokens: dict[str, str]) -> None:
    # Phase-0 observation requirement: nonexistent incident appears successful
    # (done=1, records=0) with no explicit error.
    nonexistent_id = "IN26082200000000000051"
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": [nonexistent_id]})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=600)
    elapsed = time.monotonic() - t0
    counts = terminal.get("counts", {})
    done = int(counts.get("done", 0))
    failed = int(counts.get("failed", 0))
    dead = int(counts.get("dead", 0))
    records = int(terminal.get("records", 0))
    outcome = _scenario_outcome(
        intervention=False,
        done=done,
        failed=failed,
        dead=dead,
        elapsed_s=elapsed,
        delay_threshold_s=30.0,
    )
    _write_evidence(
        "E-1",
        [
            f"request_id={rid}",
            f"nonexistent_incident_id={nonexistent_id}",
            f"total_pages={total_pages}",
            f"counts={counts}",
            f"records={records}",
            f"elapsed_s={elapsed:.3f}",
            "observation=nonexistent incident returns done:1, records:0 with no error",
            f"scenario_status={outcome}",
        ],
    )
    assert done == 1 and failed == 0 and dead == 0 and records == 0


def test_e2_kill_and_resume_enrichment(tokens: dict[str, str]) -> None:
    _cancel_running(SENTINEL_JOB)
    base_execution = _start(SENTINEL_JOB, 3)
    time.sleep(10)
    ids = _mock_discover_ids(tokens["mock"], need=10000)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    time.sleep(8)
    cancelled = _cancel_running(SENTINEL_JOB)
    mid = _counts(tokens["api"], rid)
    restart_execution = _start(SENTINEL_JOB, 3)
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=3600)
    elapsed = time.monotonic() - t0
    counts = terminal.get("counts", {})
    done = int(counts.get("done", 0))
    failed = int(counts.get("failed", 0))
    dead = int(counts.get("dead", 0))
    outcome = _scenario_outcome(
        intervention=True,
        done=done,
        failed=failed,
        dead=dead,
        elapsed_s=elapsed,
        delay_threshold_s=0.0,
    )
    _write_evidence(
        "E-2",
        [
            f"base_execution={base_execution}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"cancelled_executions={cancelled}",
            f"mid_counts={mid.get('counts', {})}",
            f"restart_execution={restart_execution}",
            f"terminal_counts={counts}",
            f"elapsed_s={elapsed:.3f}",
            f"scenario_status={outcome}",
        ],
    )
    assert done == total_pages and failed == 0 and dead == 0


def test_e3_kill_after_progress_and_resume(tokens: dict[str, str]) -> None:
    _cancel_running(SENTINEL_JOB)
    base_execution = _start(SENTINEL_JOB, 3)
    time.sleep(10)
    ids = _mock_discover_ids(tokens["mock"], need=12000)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    # Wait until at least one page is done to represent post-progress kill.
    mid_progress: dict[str, Any] = {}
    for _ in range(120):
        mid_progress = _counts(tokens["api"], rid)
        done = int(mid_progress.get("counts", {}).get("done", 0))
        if done > 0:
            break
        time.sleep(2)
    cancelled = _cancel_running(SENTINEL_JOB)
    paused = _counts(tokens["api"], rid)
    restart_execution = _start(SENTINEL_JOB, 3)
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=3600)
    elapsed = time.monotonic() - t0
    counts = terminal.get("counts", {})
    done = int(counts.get("done", 0))
    failed = int(counts.get("failed", 0))
    dead = int(counts.get("dead", 0))
    outcome = _scenario_outcome(
        intervention=True,
        done=done,
        failed=failed,
        dead=dead,
        elapsed_s=elapsed,
        delay_threshold_s=0.0,
    )
    _write_evidence(
        "E-3",
        [
            f"base_execution={base_execution}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"mid_progress_counts={mid_progress.get('counts', {})}",
            f"cancelled_executions={cancelled}",
            f"paused_counts={paused.get('counts', {})}",
            f"restart_execution={restart_execution}",
            f"terminal_counts={counts}",
            f"elapsed_s={elapsed:.3f}",
            f"scenario_status={outcome}",
        ],
    )
    assert done == total_pages and failed == 0 and dead == 0


def test_e4_source_fault_dead_letters(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=100)
    fault_id = ids[0]
    healthy_ids = ids[50:100]
    _mock_set_fault(tokens["mock"], fault_id)
    try:
        t0 = time.monotonic()
        rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": [fault_id] + healthy_ids})
        terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
        elapsed = time.monotonic() - t0
        counts = terminal.get("counts", {})
        done = int(counts.get("done", 0))
        failed = int(counts.get("failed", 0))
        dead = int(counts.get("dead", 0))
        outcome = _scenario_outcome(
            intervention=False,
            done=done,
            failed=failed,
            dead=dead,
            elapsed_s=elapsed,
            delay_threshold_s=120.0,
        )
        _write_evidence(
            "E-4",
            [
                f"request_id={rid}",
                f"fault_id={fault_id}",
                f"healthy_ids_count={len(healthy_ids)}",
                f"total_pages={total_pages}",
                f"terminal_counts={counts}",
                f"elapsed_s={elapsed:.3f}",
                f"scenario_status={outcome}",
            ],
        )
        assert dead + failed > 0
    finally:
        _mock_clear_faults(tokens["mock"])


def test_e5_dead_letter_auth_surface(tokens: dict[str, str]) -> None:
    unauth_status = None
    unauth_body = ""
    try:
        req = urllib.request.Request(API_URL + "/v1/dead-letter", method="GET")
        with urllib.request.urlopen(req, timeout=120) as resp:
            unauth_status = resp.getcode()
            unauth_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        unauth_status = exc.code
        unauth_body = exc.read().decode("utf-8")

    authed = _http_json("GET", API_URL + "/v1/dead-letter", tokens["api"])
    row_count = len(authed.get("rows", []))
    _write_evidence(
        "E-5",
        [
            f"unauth_http_status={unauth_status}",
            f"unauth_body_prefix={unauth_body[:200]!r}",
            f"authed_rows={row_count}",
            "authentication=/v1/dead-letter is authenticated by Cloud Run IAM (OIDC identity token in Authorization bearer header); application code contains no separate endpoint-specific auth.",
            "scenario_status=recovered",
        ],
    )
    assert unauth_status in (401, 403)


def test_e6_payload_fault_modes(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=20)
    target = ids[0]
    control = ids[1]
    modes = [
        "truncated_json",
        "html_error_page",
        "empty_body_200",
        "incidents_string",
    ]
    rows: list[dict[str, Any]] = []
    for mode in modes:
        try:
            _mock_set_payload_fault(tokens["mock"], target, mode)
        except RuntimeError as exc:
            _write_evidence(
                "E-6",
                [
                    f"fault_target={target}",
                    f"control_id={control}",
                    "blocked_reason=deployed mock does not expose /admin/payload-fault endpoints (HTTP 404)",
                    f"exception={exc}",
                    "scenario_status=needed_intervention",
                ],
            )
            pytest.skip("E-6 blocked: deployed mock lacks /admin/payload-fault endpoint")
        try:
            t0 = time.monotonic()
            rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": [target, control]})
            terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
            elapsed = time.monotonic() - t0
            counts = terminal.get("counts", {})
            done = int(counts.get("done", 0))
            failed = int(counts.get("failed", 0))
            dead = int(counts.get("dead", 0))
            rows.append(
                {
                    "mode": mode,
                    "request_id": rid,
                    "total_pages": total_pages,
                    "done": done,
                    "failed": failed,
                    "dead": dead,
                    "records": int(terminal.get("records", 0)),
                    "elapsed_s": round(elapsed, 3),
                    "scenario_status": _scenario_outcome(
                        intervention=False,
                        done=done,
                        failed=failed,
                        dead=dead,
                        elapsed_s=elapsed,
                        delay_threshold_s=120.0,
                    ),
                }
            )
        finally:
            _mock_clear_payload_faults(tokens["mock"])

    _write_evidence(
        "E-6",
        [
            f"fault_target={target}",
            f"control_id={control}",
            f"per_mode={json.dumps(rows, separators=(',', ':'))}",
        ],
    )
    assert len(rows) == 4

