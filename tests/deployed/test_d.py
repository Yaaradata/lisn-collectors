from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from google.cloud import bigquery
from google.cloud.sql.connector import Connector

API_URL = "https://collector-api-mfo5qzthxa-el.a.run.app"
MOCK_URL = "https://mock-sentinel-mfo5qzthxa-el.a.run.app"
PROJECT = "clariversev1"
REGION = "asia-south1"
SENTINEL_JOB = "col-sentinel"
DISCOVERY_JOB = "col-sentinel-discovery"
MAINT_JOB = "col-maintenance"
EVIDENCE_DIR = Path(__file__).resolve().parent / "evidence"


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


def test_single_page_happy_path(tokens: dict[str, str]) -> None:
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
        "single_page_happy_path",
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


def test_multi_page_happy_path(tokens: dict[str, str]) -> None:
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
        "multi_page_happy_path",
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


def test_bulk_enrichment_completion(tokens: dict[str, str]) -> None:
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
        "bulk_enrichment_completion",
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


def test_discovery_to_enrichment_bridge(tokens: dict[str, str]) -> None:
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
        "discovery_to_enrichment_bridge",
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


def test_single_request_latency_while_sweeping(tokens: dict[str, str]) -> None:
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
        "single_request_latency_while_sweeping",
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


def test_sentinel_worker_cancel_and_manual_restart(tokens: dict[str, str]) -> None:
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
        "sentinel_worker_cancel_and_manual_restart",
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
            "note=recovery observed only after manual restart by this test",
        ],
    )
    assert not lost


def test_discovery_worker_cancel_and_manual_restart(tokens: dict[str, str]) -> None:
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
        "discovery_worker_cancel_and_manual_restart",
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
            "note=recovery observed only after manual restart by this test",
        ],
    )
    assert len(observed) > 0


def test_large_run_to_conclusion_30m_cap(tokens: dict[str, str]) -> None:
    ids = _mock_discover_ids(tokens["mock"], need=60000)
    t0 = time.monotonic()
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
    elapsed = time.monotonic() - t0
    assert elapsed <= 1800.0, f"large run exceeded 30-minute cap: {elapsed:.3f}s"
    sample_ids = ids[:2000]
    truth = _mock_identities(tokens["mock"], sample_ids)
    observed = _bq_identities_for_request_and_ids(rid, sample_ids)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=elapsed, delay_threshold_s=600.0)
    _write_evidence(
        "large_run_to_conclusion_30m_cap",
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


def test_hold_240s_sampling_15s(tokens: dict[str, str]) -> None:
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
    assert total_elapsed >= 240.0, f"hold floor violated: {total_elapsed:.3f}s"
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=1800)
    sample_ids = ids[:2000]
    observed = _bq_identities_for_request_and_ids(rid, sample_ids)
    truth = _mock_identities(tokens["mock"], sample_ids)
    lost = observed != truth
    status = _status_label(intervention=False, lost_data=lost, elapsed_s=total_elapsed, delay_threshold_s=240.0)
    _write_evidence(
        "hold_240s_sampling_15s",
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
            "note=no killswitch pause asserted — observation only",
        ],
    )
    assert not lost


# ---------------------------------------------------------------------------
# Protocol-matching scenarios (see tests/deployed/COVERAGE.md)
# ---------------------------------------------------------------------------


def _mock_scale_to_zero() -> None:
    """Protocol D-6: take the mock offline by pinning max instances to 0."""
    _run(
        [
            "gcloud",
            "run",
            "services",
            "update",
            "mock-sentinel",
            f"--region={REGION}",
            f"--project={PROJECT}",
            "--min-instances=0",
            "--max-instances=0",
            "--quiet",
        ]
    )


def _mock_restore_scale() -> None:
    _run(
        [
            "gcloud",
            "run",
            "services",
            "update",
            "mock-sentinel",
            f"--region={REGION}",
            f"--project={PROJECT}",
            "--min-instances=1",
            "--max-instances=20",
            "--quiet",
        ]
    )


def _mock_health_ok(mock_token: str, timeout_s: float = 5.0) -> bool:
    try:
        body = json.dumps(None)
        req = urllib.request.Request(
            MOCK_URL + "/health",
            data=None,
            headers={"Authorization": f"Bearer {mock_token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status == 200
    except Exception:
        return False


def _sql_connect():
    conn_name = os.environ.get("CONN")
    dbpw = os.environ.get("DBPW")
    if not conn_name or not dbpw:
        pytest.skip("CONN/DBPW required for killswitch / identity SQL checks")
    connector = Connector()
    conn = connector.connect(
        conn_name, "pg8000", user="postgres", password=dbpw, db="collector"
    )
    return connector, conn


def _killswitch_set(source: str, paused: bool) -> None:
    connector, conn = _sql_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS collector_control (
              source text PRIMARY KEY,
              paused boolean NOT NULL DEFAULT false,
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            INSERT INTO collector_control (source, paused, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (source) DO UPDATE
              SET paused = EXCLUDED.paused, updated_at = now()
            """,
            (source, paused),
        )
        conn.commit()
    finally:
        conn.close()
        connector.close()


def _missing_keys_for_request(request_id: str) -> list[dict[str, Any]]:
    connector, conn = _sql_connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT job_id::text, missing_keys
            FROM collector_job
            WHERE request_id = %s::uuid
              AND missing_keys IS NOT NULL
            """,
            (request_id,),
        )
        rows = []
        for job_id, mk in cur.fetchall():
            payload = mk
            if isinstance(payload, str):
                payload = json.loads(payload)
            rows.append({"job_id": job_id, "missing_keys": payload})
        return rows
    finally:
        conn.close()
        connector.close()


def _mock_stats(mock_token: str) -> int:
    return int(_http_json("GET", MOCK_URL + "/admin/stats", mock_token).get("requests", 0))


def test_d6_source_down_60s(tokens: dict[str, str]) -> None:
    """Protocol D-6: mock scaled to zero for 60s; pages retry and complete."""
    _cancel_running(SENTINEL_JOB)
    base_exec = _start(SENTINEL_JOB, 3)
    time.sleep(12)
    ids = _mock_discover_ids(tokens["mock"], need=200)
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    time.sleep(3)
    down_at = time.monotonic()
    confirmed_down = False
    try:
        _mock_scale_to_zero()
        # Wait until health fails (instances gone), then hold remaining time to 60s.
        deadline = down_at + 90.0
        while time.monotonic() < deadline:
            if not _mock_health_ok(tokens["mock"]):
                confirmed_down = True
                break
            time.sleep(2)
        assert confirmed_down, "mock still healthy after scale-to-zero"
        # Hold from first confirmed-down observation for a full 60s.
        hold_start = time.monotonic()
        while time.monotonic() - hold_start < 60.0:
            time.sleep(5)
            assert not _mock_health_ok(tokens["mock"]), "mock came back during hold"
        down_held = time.monotonic() - hold_start
    finally:
        _mock_restore_scale()
        # Wait for mock to accept traffic again.
        ready_deadline = time.monotonic() + 180.0
        while time.monotonic() < ready_deadline:
            if _mock_health_ok(tokens["mock"], timeout_s=10.0):
                break
            time.sleep(5)
        else:
            raise AssertionError("mock did not recover health after restore")
    assert down_held >= 60.0, f"source-down hold too short: {down_held:.3f}s"
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=3600)
    done = int(terminal.get("counts", {}).get("done", 0))
    dead = int(terminal.get("counts", {}).get("dead", 0))
    _write_evidence(
        "D-6",
        [
            f"base_execution={base_exec}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"source_down_held_s={down_held:.3f}",
            f"confirmed_down={confirmed_down}",
            f"terminal_counts={terminal.get('counts', {})}",
            "method=gcloud run services update --min-instances=0 --max-instances=0",
            f"scenario_status={'recovered_with_delay' if done == total_pages else 'needed_intervention'}",
        ],
    )
    assert done == total_pages and dead == 0


def test_d9_unrequested_records(tokens: dict[str, str]) -> None:
    """Protocol D-9: source returns an id that was not requested; Pass 4 detects it."""
    ids = _mock_discover_ids(tokens["mock"], need=5)
    target = ids[0]
    # Clear any prior payload faults, then inject unrequested_extra.
    try:
        _http_json("DELETE", MOCK_URL + "/admin/payload-fault", tokens["mock"])
    except Exception:
        pass
    set_resp = _http_json(
        "POST",
        MOCK_URL + f"/admin/payload-fault/{target}/unrequested_extra",
        tokens["mock"],
    )
    assert "unrequested_extra" in str(set_resp.get("payload_faults", {})), set_resp
    try:
        rid, total_pages = _collect(
            tokens["api"], "sentinel", {"incident_ids": [target]}
        )
        terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=900)
        requested = int(terminal.get("requested", 0))
        returned = int(terminal.get("returned", 0))
        mk_rows = _missing_keys_for_request(rid)
        unexpected_total = sum(
            int((r["missing_keys"] or {}).get("unexpected_total") or 0) for r in mk_rows
        )
        unexpected_samples = [
            (r["missing_keys"] or {}).get("unexpected") for r in mk_rows
        ]
        _write_evidence(
            "D-9",
            [
                f"request_id={rid}",
                f"fault_target={target}",
                f"terminal_counts={terminal.get('counts', {})}",
                f"requested={requested}",
                f"returned={returned}",
                f"missing_field_on_counts={terminal.get('missing')}",
                f"unexpected_total={unexpected_total}",
                f"unexpected_samples={json.dumps(unexpected_samples)}",
                "detection=Pass 4 shortfall_keys unexpected + counts returned>requested",
            ],
        )
        assert returned > requested, (
            f"Pass 4 comparison did not surface extras: "
            f"requested={requested} returned={returned}"
        )
        assert unexpected_total >= 1, f"missing_keys had no unexpected: {mk_rows}"
    finally:
        _http_json("DELETE", MOCK_URL + "/admin/payload-fault", tokens["mock"])


def test_d12_killswitch_pause_240s(tokens: dict[str, str]) -> None:
    """Protocol D-12: pause with ≥300 pages queued; zero source calls for 240s."""
    _cancel_running(SENTINEL_JOB)
    base_exec = _start(SENTINEL_JOB, 3)
    time.sleep(12)
    # 50 ids/page → 300 pages needs 15_000 ids.
    ids = _mock_discover_ids(tokens["mock"], need=15000)
    rid, total_pages = _collect(tokens["api"], "sentinel", {"incident_ids": ids})
    assert total_pages >= 300, f"need ≥300 pages queued, got {total_pages}"
    # Let a few pages start so workers are hot, then pause.
    time.sleep(8)
    _killswitch_set("sentinel", True)
    samples: list[dict[str, Any]] = []
    try:
        before = _mock_stats(tokens["mock"])
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= 240.0:
                break
            after = _mock_stats(tokens["mock"])
            samples.append(
                {
                    "t_s": round(elapsed, 3),
                    "mock_requests": after,
                    "delta_from_pause_start": after - before,
                    "request_counts": _counts(tokens["api"], rid).get("counts", {}),
                }
            )
            time.sleep(15)
        hold_elapsed = time.monotonic() - start
        after_hold = _mock_stats(tokens["mock"])
        calls_while_paused = after_hold - before
    finally:
        _killswitch_set("sentinel", False)

    assert hold_elapsed >= 240.0, f"D-12 floor violated: {hold_elapsed:.3f}s"
    # In-flight HTTP at pause instant may complete; allow a tiny slop (≤3).
    assert calls_while_paused <= 3, (
        f"expected ~0 source calls while paused, got {calls_while_paused}; "
        f"samples={samples[:3]}…{samples[-1:]}"
    )
    terminal = _wait_terminal(tokens["api"], rid, total_pages, timeout_s=5400)
    done = int(terminal.get("counts", {}).get("done", 0))
    _write_evidence(
        "D-12",
        [
            f"base_execution={base_exec}",
            f"request_id={rid}",
            f"total_pages={total_pages}",
            f"hold_elapsed_s={hold_elapsed:.3f}",
            f"mock_requests_at_pause={before}",
            f"mock_requests_after_hold={after_hold}",
            f"calls_while_paused={calls_while_paused}",
            f"sample_count={len(samples)}",
            f"samples_json={json.dumps(samples, separators=(',', ':'))}",
            f"terminal_counts={terminal.get('counts', {})}",
            f"scenario_status={'recovered' if done == total_pages else 'needed_intervention'}",
        ],
    )
    assert done == total_pages


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup() -> Any:
    # Keep this section off D-4, D-5, D-10 by construction (not implemented),
    # and never call any admin delete endpoint.
    yield
    try:
        _killswitch_set("sentinel", False)
    except Exception:
        pass
    try:
        _mock_restore_scale()
    except Exception:
        pass
    _cancel_running(SENTINEL_JOB)
    _cancel_running(DISCOVERY_JOB)
    _ensure_workers_baseline()

