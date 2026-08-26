from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import psycopg

from collector.app import app as procrastinate_app
from collector.tasks import sweep
from tests.acceptance.helpers import (
    dataset_by_code,
    incident_ids_from_identity_set,
    post_collect,
    reset_collector_state,
    wait_request_terminal,
    write_evidence,
)

UTC = timezone.utc


def _api_request(
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    *,
    timeout_s: int = 60,
) -> tuple[int, dict[str, Any]]:
    url = f"http://127.0.0.1:8080{path}"
    if query:
        encoded = urllib.parse.urlencode(query, doseq=True)
        url = f"{url}?{encoded}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload


def _window_iso(start: datetime, end: datetime) -> dict[str, str | int]:
    return {
        "updated_from": start.isoformat().replace("+00:00", "Z"),
        "updated_to": end.isoformat().replace("+00:00", "Z"),
        "limit": 1000,
    }


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _mock_dsn() -> str:
    return os.environ["SENTINEL_MOCK_DSN"]


def _run_sweep_now() -> dict[str, Any]:
    async def _run() -> dict[str, Any]:
        async with procrastinate_app.open_async():
            return await sweep(int(datetime.now(tz=UTC).timestamp()))

    return asyncio.run(_run())


def _populate_outputs() -> dict[str, str]:
    ds1 = dataset_by_code("DS-1")
    ds1.build()
    incident_ids = incident_ids_from_identity_set(
        {(str(row["id"]), None if row.get("thread_id") is None else str(row.get("thread_id"))) for row in ds1.truth()}
    )
    sentinel_request_id = post_collect("sentinel", {"incident_ids": incident_ids[:100]})
    wait_request_terminal(sentinel_request_id, timeout_s=300)

    ds11 = dataset_by_code("DS-11")
    snap = ds11.build()
    discovery_request_id = post_collect(
        "sentinel_discovery",
        _window_iso(snap.min_updated_on, snap.max_updated_on),
    )
    wait_request_terminal(discovery_request_id, timeout_s=300)
    return {"sentinel_request_id": sentinel_request_id, "discovery_request_id": discovery_request_id}


def test_g5_dead_letter_exposes_forced_dsn_exception() -> None:
    reset_collector_state()
    ds1 = dataset_by_code("DS-1")
    ds1.build()
    incident_ids = incident_ids_from_identity_set(
        {(str(row["id"]), None if row.get("thread_id") is None else str(row.get("thread_id"))) for row in ds1.truth()}
    )
    request_id = post_collect("sentinel", {"incident_ids": incident_ids[:50]})
    wait_request_terminal(request_id, timeout_s=180)

    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id::text
                FROM collector_job
                WHERE request_id = %s::uuid
                ORDER BY page_no
                LIMIT 1
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise AssertionError("collector job row missing for G-5 setup")
            job_id = str(row[0])
            try:
                raise RuntimeError(f"forced DSN exception: {_mock_dsn()}")
            except RuntimeError as exc:
                forced_error = str(exc)
            cur.execute(
                """
                UPDATE collector_job
                SET status='in_progress',
                    attempts=5,
                    lease_expires_at=now() - interval '2 minutes',
                    last_error=%s,
                    updated_at=now()
                WHERE job_id = %s::uuid
                """,
                (forced_error, job_id),
            )
        conn.commit()

    sweep_result = _run_sweep_now()
    status, dead_letter = _api_request("GET", "/v1/dead-letter")
    matching = [r for r in dead_letter.get("rows", []) if r.get("job_id") == job_id]
    write_evidence(
        "G-5",
        [
            f"request_id={request_id}",
            f"job_id={job_id}",
            f"forced_error={forced_error}",
            f"sweep_result={json.dumps(sweep_result, sort_keys=True)}",
            f"dead_letter_status={status}",
            f"dead_letter_dead={dead_letter.get('dead')}",
            f"dead_letter_match={json.dumps(matching, sort_keys=True)}",
        ],
    )
    assert status == 200
    assert matching, "dead-letter did not expose forced exception row"
    assert forced_error in str(matching[0].get("last_error", ""))


def test_g6_admin_reset_guards_confirm_and_dry_run_snapshot() -> None:
    reset_collector_state()
    setup_ids = _populate_outputs()

    bad_status, bad_payload = _api_request(
        "DELETE",
        "/v1/admin/collector-data",
        {"confirm": "wrong-confirm", "dry_run": "true"},
    )
    preview_status, preview = _api_request(
        "DELETE",
        "/v1/admin/collector-data",
        {"confirm": "reset-collector-data", "dry_run": "true"},
    )
    write_evidence(
        "G-6",
        [
            f"setup={json.dumps(setup_ids, sort_keys=True)}",
            f"bad_confirm_status={bad_status}",
            f"bad_confirm_payload={json.dumps(bad_payload, sort_keys=True)}",
            f"preview_status={preview_status}",
            f"preview_dry_run={preview.get('dry_run')}",
            f"preview_success={preview.get('success')}",
            f"preview_cleared={json.dumps(preview.get('cleared', {}), sort_keys=True)}",
            f"preview_preserved={json.dumps(preview.get('preserved', {}), sort_keys=True)}",
            f"preview_warnings={json.dumps(preview.get('warnings', []), sort_keys=True)}",
        ],
    )
    assert bad_status == 400
    assert preview_status == 200
    assert bool(preview.get("dry_run")) is True
    assert "cleared" in preview
    assert "preserved" in preview
    assert "collector_job" in preview["cleared"]
    assert "raw_manifest" in preview["cleared"]
    assert "collector_request" in preview["cleared"]
    assert "gcs_objects" in preview["cleared"]
    assert "sentinel_raw.incidents" in preview["cleared"]
    assert "sentinel_raw.discovered_ids" in preview["cleared"]


def test_g7_admin_reset_in_progress_guard_then_forced_delete() -> None:
    reset_collector_state()
    setup_ids = _populate_outputs()
    _, before = _api_request(
        "DELETE",
        "/v1/admin/collector-data",
        {"confirm": "reset-collector-data", "dry_run": "true"},
    )

    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_job
                SET status='in_progress',
                    lease_expires_at=now() + interval '5 minutes',
                    updated_at=now()
                WHERE request_id = %s::uuid
                RETURNING job_id::text
                """,
                (setup_ids["sentinel_request_id"],),
            )
            rows = cur.fetchall()
        conn.commit()
    if not rows:
        raise AssertionError("G-7 setup failed: no job rows marked in_progress")

    blocked_status, blocked_payload = _api_request(
        "DELETE",
        "/v1/admin/collector-data",
        {
            "confirm": "reset-collector-data",
            "dry_run": "false",
            "force": "false",
        },
    )
    forced_status, forced_payload = _api_request(
        "DELETE",
        "/v1/admin/collector-data",
        {
            "confirm": "reset-collector-data",
            "dry_run": "false",
            "force": "true",
        },
        timeout_s=600,
    )
    state_status, state_after = _api_request("GET", "/v1/admin/state")
    write_evidence(
        "G-7",
        [
            f"setup={json.dumps(setup_ids, sort_keys=True)}",
            f"before_snapshot={json.dumps(before.get('cleared', {}), sort_keys=True)}",
            f"blocked_status={blocked_status}",
            f"blocked_payload={json.dumps(blocked_payload, sort_keys=True)}",
            f"forced_status={forced_status}",
            f"forced_payload={json.dumps(forced_payload, sort_keys=True)}",
            f"state_status={state_status}",
            f"state_after={json.dumps(state_after, sort_keys=True)}",
        ],
    )

    assert blocked_status == 409
    detail = blocked_payload.get("detail", {})
    assert isinstance(detail, dict)
    assert int(detail.get("in_progress", 0)) > 0

    assert forced_status == 200
    assert forced_payload.get("dry_run") is False
    assert state_status == 200
    cloud_sql = state_after.get("cloud_sql", {})
    assert int(cloud_sql.get("collector_job", 0)) == 0
    assert int(cloud_sql.get("collector_request", 0)) == 0
    assert int(cloud_sql.get("raw_manifest", 0)) == 0
    bigquery = state_after.get("bigquery", {})
    assert int(bigquery.get("sentinel_raw.incidents", 0)) == 0
    assert int(bigquery.get("sentinel_raw.discovered_ids", 0)) == 0
    assert int(state_after.get("gcs_objects_raw", 0)) == 0
