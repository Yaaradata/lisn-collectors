"""Section H tests (request API robustness)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from collector.db import connect
from tests.audit.helpers import fetch_incident_ids, write_evidence


def test_h01_authentication_surface() -> None:
    test_id = "H-01"
    endpoints = [
        ("GET", "/health", None),
        ("GET", "/v1/counts", None),
        ("GET", "/v1/dead-letter", None),
        ("GET", "/v1/reconcile?minutes=0", None),
        ("POST", "/v1/collect", {"source": "sentinel", "query_spec": {"incident_ids": ["IN1"]}}),
    ]
    lines = []
    client = httpx.Client(base_url="http://127.0.0.1:8080", timeout=30)
    for method, path, body in endpoints:
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json=body)
        lines.append(f"{method} {path} status={resp.status_code}")
    write_evidence(test_id, lines)
    assert all("status=401" in line or "status=403" in line for line in lines)


def test_h02_error_message_redaction() -> None:
    test_id = "H-02"
    source = open("/workspace/collector/tasks.py", encoding="utf-8").read()
    api = open("/workspace/collector/api.py", encoding="utf-8").read()
    write_evidence(
        test_id,
        [
            f"stores_str_exc={'str(exc)[:4000]' in source}",
            f"dead_letter_returns_last_error={'last_error' in api}",
        ],
    )
    assert "str(exc)[:4000]" not in source


def test_h03_request_completion_signal(require_pipeline_smoke: None) -> None:
    test_id = "H-03"
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT request_id::text, status, closed_at
                FROM collector_request
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    write_evidence(test_id, [f"latest_request={row}"])
    assert row is not None
    assert row[1] != "open"
    assert row[2] is not None


def test_h04_malformed_path_parameters() -> None:
    test_id = "H-04"
    bad = httpx.get("http://127.0.0.1:8080/v1/requests/not-a-uuid/counts", timeout=30)
    unknown = httpx.get(
        f"http://127.0.0.1:8080/v1/requests/{uuid.uuid4()}/counts",
        timeout=30,
    )
    write_evidence(
        test_id,
        [
            f"not-a-uuid status={bad.status_code} body={bad.text}",
            f"unknown status={unknown.status_code} body={unknown.text}",
        ],
    )
    assert bad.status_code == 400
    assert unknown.status_code == 404


def test_h05_replay_of_identical_request() -> None:
    test_id = "H-05"
    ids = fetch_incident_ids(10)
    payload = {"source": "sentinel", "query_spec": {"incident_ids": ids}}
    r1 = httpx.post("http://127.0.0.1:8080/v1/collect", json=payload, timeout=30)
    r2 = httpx.post("http://127.0.0.1:8080/v1/collect", json=payload, timeout=30)
    write_evidence(
        test_id,
        [
            f"first_status={r1.status_code} body={r1.text}",
            f"second_status={r2.status_code} body={r2.text}",
        ],
    )
    assert r2.status_code in (409, 429), "replay should be rejected or idempotency-enforced"


def test_h06_partial_defer() -> None:
    test_id = "H-06"
    write_evidence(
        test_id,
        [
            "BLOCKED: requires controlled API crash between collector_job inserts and defer loop.",
            "missing_precondition=instrumented_api_process_or_fault_injection_hook",
        ],
    )
    pytest.skip("BLOCKED: missing controlled partial-defer fault injection precondition")
