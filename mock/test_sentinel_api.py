#!/usr/bin/env python3
"""Four load-bearing checks against the mock Sentinel API (localhost:8081).

Expects `make mock-run` (or equivalent uvicorn) to already be serving.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import psycopg

BASE = os.environ.get("MOCK_SENTINEL_URL", "http://127.0.0.1:8081")


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'").strip('"')


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def ok(msg: str) -> None:
    print(f"PASS: {msg}")


def main() -> int:
    _load_dotenv()
    client = httpx.Client(base_url=BASE, timeout=30.0)

    # 1 — health
    r = client.get("/health")
    if r.status_code != 200 or r.json().get("status") != "ok":
        fail(f"health: {r.status_code} {r.text}")
    ok(f"health status=ok incidents={r.json().get('incidents')}")

    # 2 — empty keys -> 400
    r = client.post("/v1/incidents/search", json={})
    if r.status_code != 400 or "incident_ids or order_ids required" not in r.text:
        fail(f"empty search: {r.status_code} {r.text}")
    ok("empty search rejected with 400")

    # 3 — >50 ids -> 400
    r = client.post(
        "/v1/incidents/search",
        json={"incident_ids": [f"IN{i:026d}" for i in range(51)]},
    )
    if r.status_code != 400 or "max 50 ids per call" not in r.text:
        fail(f"oversize search: {r.status_code} {r.text}")
    ok("51 ids rejected with 400")

    # 4 — search returns thread-exploded rows; fault injection returns 500
    dsn = os.environ.get("SENTINEL_MOCK_DSN")
    if not dsn:
        fail("SENTINEL_MOCK_DSN required for sample id lookup")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id
                FROM sentinel_incident i
                JOIN sentinel_thread t ON t.incident_id = i.id
                GROUP BY i.id
                HAVING count(*) >= 2
                ORDER BY i.id
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        fail("no multi-thread incident found — run seed first")
    sample_id = row[0]

    r = client.post("/v1/incidents/search", json={"incident_ids": [sample_id]})
    if r.status_code != 200:
        fail(f"search: {r.status_code} {r.text}")
    payload = r.json()
    if payload.get("count", 0) < 2:
        fail(f"expected thread explosion, count={payload.get('count')}")
    if "issue.name" not in payload["incidents"][0]:
        fail("response missing dotted export field issue.name")
    ok(f"search thread explosion count={payload['count']} id={sample_id}")

    client.post(f"/admin/fault/{sample_id}")
    r = client.post("/v1/incidents/search", json={"incident_ids": [sample_id]})
    if r.status_code != 500 or "injected fault" not in r.text:
        fail(f"fault inject: {r.status_code} {r.text}")
    client.delete("/admin/fault")
    ok("fault injection returns 500 and clears")

    print("mock-test: all four checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
