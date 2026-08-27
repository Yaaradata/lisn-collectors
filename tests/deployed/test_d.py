from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from google.cloud import bigquery

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
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
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


def _ensure_workers_baseline() -> dict[str, str]:
    return {
        "sentinel": _start(SENTINEL_JOB, 3),
        "discovery": _start(DISCOVERY_JOB, 1),
        "maintenance": _start(MAINT_JOB, 1),
    }


def _collect(api_token: str, source: str, query_spec: dict[str, Any]) -> tuple[str, int]:
    resp = _http_json("POST", API_URL + "/v1/collect", api_token, {"source": source, "query_spec": query_spec})
    rid = str(resp["request_id"])
    total_pages = int(resp.get("total_pages") or 0)
    return rid, total_pages


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


def _health_detail(api_token: str) -> dict[str, Any]:
    return _http_json("GET", API_URL + "/v1/health/detail", api_token)


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
        raise AssertionError(f"mock discovered {len(out)} ids, need {need}")
    return out[:need]


def _mock_identities(mock_token: str, incident_ids: list[str]) -> set[tuple[str, str | None]]:
    out: set[tuple[str, str | None]] = set()
    for i in range(0, len(incident_ids), 50):
        chunk = incident_ids[i : i + 50]
        page = _http_json("POST", MOCK_URL + "/v1/incidents/search", mock_token, {"incident_ids": chunk})
        for row in page.get("incidents", []):
            inc_id = str(row["id"])
            thread_val = row.get("threads.id")
            out.add((inc_id, str(thread_val) if thread_val is not None else None))
    return out


def _bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def _bq_identities_for_request(request_id: str) -> set[tuple[str, str | None]]:
    query = f"""
        SELECT DISTINCT id, threads_id
        FROM `{PROJECT}.sentinel_raw.incidents`
        WHERE _request_id = @rid
    """
    rows = _bq_client().query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
        ),
        location=REGION,
    ).result()
    return {(str(r.id), str(r.threads_id) if r.threads_id is not None else None) for r in rows}


def _bq_discovered_ids_for_request(request_id: str) -> set[str]:
    query = f"""
        SELECT DISTINCT incident_id
        FROM `{PROJECT}.sentinel_raw.discovered_ids`
        WHERE _request_id = @rid
    """
    rows = _bq_client().query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
        ),
        location=REGION,
    ).result()
    return {str(r.incident_id) for r in rows if r.incident_id is not None}


def _bq_identities_for_request_and_ids(request_id: str, ids: list[str]) -> set[tuple[str, str | None]]:
    if not ids:
        return set()
    query = f"""
        SELECT DISTINCT id, threads_id
        FROM `{PROJECT}.sentinel_raw.incidents`
        WHERE _request_id = @rid
          AND id IN UNNEST(@ids)
    """
    rows = _bq_client().query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("rid", "STRING", request_id),
                bigquery.ArrayQueryParameter("ids", "STRING", ids),
            ]
        ),
        location=REGION,
    ).result()
    return {(str(r.id), str(r.threads_id) if r.threads_id is not None else None) for r in rows}


def _bq_current_ids_for_filter(ids: set[str]) -> set[str]:
    if not ids:
        return set()
    query = f"""
        SELECT DISTINCT id
        FROM `{PROJECT}.sentinel_core.incidents_current`
        WHERE id IN UNNEST(@ids)
    """
    rows = _bq_client().query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("ids", "STRING", sorted(ids))]
        ),
        location=REGION,
    ).result()
    return {str(r.id) for r in rows if r.id is not None}


def _status_label(
    *,
    intervention: bool,
    lost_data: bool,
    elapsed_s: float,
    delay_threshold_s: float,
) -> str:
    if lost_data:
        return "lost_data"
    if intervention:
        return "needed_intervention"
    if elapsed_s > delay_threshold_s:
        return "recovered_with_delay"
    return "recovered"


@pytest.fixture(scope="session")
def tokens() -> dict[str, str]:
    return {"api": _token(API_URL), "mock": _token(MOCK_URL)}


def test_d1_single_page_recovery(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=50)
    truth = _mock_identities(tokens["mock"], ids)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=900)
    elapsed = time.monotonic() - t0
    observed = _bq_identities_for_request(rid)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=30.0)
    _write_evidence(
        "D-1",
        [
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"elapsed_s={elapsed:.3f}",
            f"truth_identities={len(truth)}",
            f"observed_identities={len(observed)}",
            f"counts={terminal.get('counts', {})}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d2_multi_page_recovery(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=500)
    truth = _mock_identities(tokens["mock"], ids)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
    elapsed = time.monotonic() - t0
    observed = _bq_identities_for_request(rid)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=60.0)
    _write_evidence(
        "D-2",
        [
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"elapsed_s={elapsed:.3f}",
            f"truth_identities={len(truth)}",
            f"observed_identities={len(observed)}",
            f"counts={terminal.get('counts', {})}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d3_bulk_recovery_with_possible_delay(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=5000)
    truth = _mock_identities(tokens["mock"], ids)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=3600)
    elapsed = time.monotonic() - t0
    observed = _bq_identities_for_request(rid)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=180.0)
    _write_evidence(
        "D-3",
        [
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"elapsed_s={elapsed:.3f}",
            f"truth_identities={len(truth)}",
            f"observed_identities={len(observed)}",
            f"counts={terminal.get('counts', {})}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d6_discovery_to_enrichment_bridge(tokens: dict[str, str]) -> None:
    t0 = time.monotonic()
    drid, dpages = _collect(
        tokens["api"],
        "sentinel_discovery",
        {"updated_from": "2026-08-22T18:00:00Z", "updated_to": "2026-08-22T19:00:00Z"},
    )
    dterm = _wait_terminal(tokens["api"], drid, dpages, timeout_s=1800)
    discovered = _bq_discovered_ids_for_request(drid)
    pending_resp = _http_json("GET", API_URL + "/v1/discovered/pending?limit=50000", tokens["api"])
    pending = set(str(x) for x in pending_resp.get("ids", []))
    pending_of_discovered = sorted(discovered & pending)
    if pending_of_discovered:
        erid, epages = _collect(tokens["api"], "sentinel", {"incident_ids": pending_of_discovered})
        eterm = _wait_terminal(tokens["api"], erid, epages, timeout_s=3600)
        enrich_counts = eterm.get("counts", {})
    else:
        erid, epages, enrich_counts = "none", 0, {}
    current = _bq_current_ids_for_filter(discovered)
    elapsed = time.monotonic() - t0
    lost = len(discovered - current) > 0
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=180.0)
    _write_evidence(
        "D-6",
        [
            f"discovery_request_id={drid}",
            f"discovery_total_pages={dpages}",
            f"discovery_counts={dterm.get('counts', {})}",
            f"discovered_ids={len(discovered)}",
            f"pending_of_discovered_before_enrich={len(pending_of_discovered)}",
            f"enrich_request_id={erid}",
            f"enrich_total_pages={epages}",
            f"enrich_counts={enrich_counts}",
            f"covered_in_incidents_current={len(current)}",
            f"elapsed_s={elapsed:.3f}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d7_single_request_latency_while_sweeping(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=20000)
    sweep_rid, sweep_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    probe_ids = ids[:50]
    t0 = time.monotonic()
    probe_rid, probe_pages = _collect(tokens["api"], "sentinel", {"incident_ids": probe_ids})
    pterm = _wait_terminal(tokens["api"], probe_rid, probe_pages, timeout_s=3600)
    probe_elapsed = time.monotonic() - t0
    sterm = _wait_terminal(tokens["api"], sweep_rid, sweep_pages, timeout_s=7200)
    truth = _mock_identities(tokens["mock"], probe_ids)
    observed = _bq_identities_for_request(probe_rid)
    lost = observed != truth
    status = _status_label(
        intervention=False,
        lost_data=lost,
        elapsed_s=probe_elapsed,
        delay_threshold_s=120.0,
    )
    _write_evidence(
        "D-7",
        [
            f"sweep_request_id={sweep_rid}",
            f"sweep_total_pages={sweep_pages}",
            f"sweep_counts={sterm.get('counts', {})}",
            f"probe_request_id={probe_rid}",
            f"probe_total_pages={probe_pages}",
            f"probe_counts={pterm.get('counts', {})}",
            f"probe_elapsed_s={probe_elapsed:.3f}",
            f"probe_truth_identities={len(truth)}",
            f"probe_observed_identities={len(observed)}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d8_sentinel_worker_intervention_required(tokens: dict[str, str]) -> None:
    _cancel_running(SENTINEL_JOB)
    base_exec = _start(SENTINEL_JOB, 3)
    time.sleep(12)
    ids = _mock_discover_ids(tokens["mock"], need=10000)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    time.sleep(8)
    cancelled = _cancel_running(SENTINEL_JOB)
    paused_counts = _counts(tokens["api"], rid)
    restart_exec = _start(SENTINEL_JOB, 3)
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=5400)
    elapsed = time.monotonic() - t0
    observed = _bq_identities_for_request(rid)
    truth = _mock_identities(tokens["mock"], ids)
    lost = observed != truth
    status = _status_label(intervention=True, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=0.0)
    _write_evidence(
        "D-8",
        [
            f"base_execution={base_exec}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"cancelled_executions={cancelled}",
            f"paused_counts={paused_counts.get('counts', {})}",
            f"restart_execution={restart_exec}",
            f"terminal_counts={terminal.get('counts', {})}",
            f"truth_identities={len(truth)}",
            f"observed_identities={len(observed)}",
            f"elapsed_s={elapsed:.3f}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d9_discovery_worker_intervention_required(tokens: dict[str, str]) -> None:
    _cancel_running(DISCOVERY_JOB)
    base_exec = _start(DISCOVERY_JOB, 1)
    time.sleep(10)
    t0 = time.monotonic()
    rid, total_pages = _collect(
        tokens["api"],
        "sentinel_discovery",
        {
            "updated_from": "2026-08-20T00:00:00Z",
            "updated_to": "2026-08-27T00:00:00Z",
            "limit": 200,
        },
    )
    time.sleep(5)
    cancelled = _cancel_running(DISCOVERY_JOB)
    paused = _counts(tokens["api"], rid)
    restart_exec = _start(DISCOVERY_JOB, 1)
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=3600)
    elapsed = time.monotonic() - t0
    observed = _bq_discovered_ids_for_request(rid)
    lost = len(observed) == 0
    status = _status_label(intervention=True, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=0.0)
    _write_evidence(
        "D-9",
        [
            f"base_execution={base_exec}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"cancelled_executions={cancelled}",
            f"paused_counts={paused.get('counts', {})}",
            f"restart_execution={restart_exec}",
            f"terminal_counts={terminal.get('counts', {})}",
            f"observed_discovered_ids={len(observed)}",
            f"elapsed_s={elapsed:.3f}",
            f"scenario_status={status}",
        ],
    )
    assert len(observed) > 0


def test_d11_run_to_conclusion_with_30m_cap(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=60000)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
    elapsed = time.monotonic() - t0
    assert elapsed <= 1800.0, f"D-11 exceeded 30-minute cap: {elapsed:.3f}s"
    sample_ids = ids[:2000]
    truth = _mock_identities(tokens["mock"], sample_ids)
    observed = _bq_identities_for_request_and_ids(rid, sample_ids)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=600.0)
    _write_evidence(
        "D-11",
        [
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"terminal_counts={terminal.get('counts', {})}",
            f"elapsed_s={elapsed:.3f}",
            f"sample_ids_checked={len(sample_ids)}",
            f"sample_truth_identities={len(truth)}",
            f"sample_observed_identities={len(observed)}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


def test_d12_hold_240s_sampling_15s(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=120000)
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    samples: list[dict[str, Any]] = []
    start = time.monotonic()
    while True:
        now = time.monotonic()
        elapsed = now - start
        if elapsed >= 240.0:
            break
        health = _health_detail(tokens["api"])
        req_counts = _counts(tokens["api"], rid)
        samples.append(
            {
                "t_s": round(elapsed, 3),
                "live_workers": health.get("live_workers"),
                "stuck": health.get("stuck"),
                "orphans": health.get("orphans"),
                "dead_jobs": health.get("dead"),
                "request_counts": req_counts.get("counts", {}),
            }
        )
        time.sleep(15)
    total_elapsed = time.monotonic() - start
    assert total_elapsed >= 240.0, f"D-12 hold floor violated: {total_elapsed:.3f}s"
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
    sample_ids = ids[:2000]
    observed = _bq_identities_for_request_and_ids(rid, sample_ids)
    truth = _mock_identities(tokens["mock"], sample_ids)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=total_elapsed, delay_threshold_s=240.0)
    _write_evidence(
        "D-12",
        [
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"hold_elapsed_s={total_elapsed:.3f}",
            f"sample_count={len(samples)}",
            f"samples_json={json.dumps(samples, separators=(',', ':'))}",
            f"terminal_counts={terminal.get('counts', {})}",
            f"sample_ids_checked={len(sample_ids)}",
            f"sample_truth_identities={len(truth)}",
            f"sample_observed_identities={len(observed)}",
            f"scenario_status={status}",
        ],
    )
    assert not lost


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup() -> Any:
    # Keep this section off D-4, D-5, D-10 by construction (not implemented),
    # and never call any admin delete endpoint.
    yield
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    _ensure_workers_baseline()

