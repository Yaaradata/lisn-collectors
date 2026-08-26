from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from google.cloud import bigquery

from tests.acceptance.datasets import DATASETS

EVIDENCE_DIR = Path("/workspace/tests/acceptance/evidence")


@dataclass(frozen=True)
class RequestOutcome:
    request_id: str
    total_pages: int
    done: int
    failed: int
    dead: int
    records: int


def _collector_dsn() -> str:
    return os.environ["COLLECTOR_DSN"]


def _mock_dsn() -> str:
    return os.environ["SENTINEL_MOCK_DSN"]


def _project() -> str:
    return os.environ["PROJECT"].strip()


def ensure_adc() -> None:
    payload = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")
    if not payload:
        return
    path = Path("/tmp/gcp-adc.json")
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


def write_evidence(test_id: str, lines: list[str]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{test_id}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def reset_collector_state() -> None:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                TRUNCATE TABLE raw_manifest, collector_job, collector_request,
                               procrastinate_events, procrastinate_jobs,
                               procrastinate_periodic_defers
                RESTART IDENTITY CASCADE
                """
            )
        conn.commit()


def post_collect_detailed(source: str, query_spec: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = {"source": source, "query_spec": query_spec}
    req = urllib.request.Request(
        "http://127.0.0.1:8080/v1/collect",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return int(resp.status), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload


def post_collect(source: str, query_spec: dict[str, Any]) -> str:
    status, payload = post_collect_detailed(source, query_spec)
    if status < 200 or status >= 300:
        raise AssertionError(f"collect failed status={status} payload={payload}")
    rid = payload.get("request_id")
    if not rid:
        raise AssertionError(f"collect succeeded but request_id missing payload={payload}")
    return str(rid)


def _request_meta(request_id: str) -> tuple[int, int, int, int]:
    with psycopg.connect(_collector_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT total_pages
                FROM collector_request
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise AssertionError(f"request not found: {request_id}")
            total_pages = int(row[0])
            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE status = 'done')::int AS done,
                  count(*) FILTER (WHERE status = 'failed')::int AS failed,
                  count(*) FILTER (WHERE status = 'dead')::int AS dead,
                  coalesce(sum(record_count), 0)::int AS records
                FROM collector_job
                WHERE request_id = %s::uuid
                """,
                (request_id,),
            )
            done, failed, dead, records = cur.fetchone()
    return total_pages, int(done), int(failed), int(dead), int(records)


def wait_request_terminal(request_id: str, timeout_s: int = 300) -> RequestOutcome:
    deadline = time.monotonic() + timeout_s
    last: tuple[int, int, int, int] | None = None
    while time.monotonic() < deadline:
        total_pages, done, failed, dead, records = _request_meta(request_id)
        last = (total_pages, done, failed, dead, records)
        if done + failed + dead >= total_pages:
            return RequestOutcome(
                request_id=request_id,
                total_pages=total_pages,
                done=done,
                failed=failed,
                dead=dead,
                records=records,
            )
        time.sleep(2)
    raise AssertionError(f"request {request_id} not terminal within {timeout_s}s last={last}")


def bq_identity_set(request_id: str) -> set[tuple[str, str | None]]:
    ensure_adc()
    client = bigquery.Client(project=_project())
    query = f"""
        SELECT id, threads_id
        FROM `{_project()}.sentinel_raw.incidents`
        WHERE _request_id = @rid
    """
    rows = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
        ),
    ).result()
    return {(str(row["id"]), None if row["threads_id"] is None else str(row["threads_id"])) for row in rows}


def dataset_by_code(code: str):
    return next(d for d in DATASETS if d.code == code)


def dataset_truth_identity_set(code: str) -> set[tuple[str, str | None]]:
    ds = dataset_by_code(code)
    rows = ds.truth()
    out: set[tuple[str, str | None]] = set()
    for row in rows:
        out.add((str(row["id"]), None if row.get("thread_id") is None else str(row.get("thread_id"))))
    return out


def incident_ids_from_identity_set(identities: set[tuple[str, str | None]]) -> list[str]:
    return sorted({incident_id for incident_id, _ in identities})


def truth_identity_set_from_mock_sql(sql: str, params: tuple[Any, ...]) -> set[tuple[str, str | None]]:
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return {(str(row[0]), None if row[1] is None else str(row[1])) for row in rows}


def mock_key_values_for_incidents(incident_ids: list[str], key_field: str) -> list[Any]:
    allowed = {"order_id", "order_item_id"}
    if key_field not in allowed:
        raise ValueError(f"unsupported key field {key_field}")
    query = f"""
        SELECT DISTINCT {key_field}
        FROM sentinel_incident
        WHERE id = ANY(%s)
          AND {key_field} IS NOT NULL
        ORDER BY {key_field}
        """
    with psycopg.connect(_mock_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (incident_ids,))
            vals = [row[0] for row in cur.fetchall()]
    if key_field == "order_item_id":
        return [int(v) for v in vals]
    return [str(v) for v in vals]
