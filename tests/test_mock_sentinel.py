"""Sprint 2 exit criteria: pytest against a running mock at :8081."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import psycopg
import pytest

BASE_URL = os.environ.get("MOCK_SENTINEL_URL", "http://127.0.0.1:8081")


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


_load_dotenv()


@pytest.fixture(scope="module")
def dsn() -> str:
    value = os.environ.get("SENTINEL_MOCK_DSN")
    if not value:
        pytest.fail("SENTINEL_MOCK_DSN is required")
    return value


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as http:
        try:
            r = http.get("/health")
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"Mock Sentinel not reachable at {BASE_URL}. "
                f"Start it with `make mock-run`. ({exc})"
            )
        yield http


@pytest.fixture(scope="module")
def multi_thread_incident_id(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id
                FROM sentinel_incident AS i
                JOIN sentinel_thread AS t ON t.incident_id = i.id
                GROUP BY i.id
                HAVING count(*) > 1
                ORDER BY i.id
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        pytest.fail("No multi-thread incident found — run `make seed` first")
    return row[0]


@pytest.fixture(scope="module")
def sample_incident_ids(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sentinel_incident ORDER BY id LIMIT 51"
            )
            rows = cur.fetchall()
    ids = [r[0] for r in rows]
    if len(ids) < 51:
        pytest.fail("Need at least 51 incidents — run `make seed` with N_INCIDENTS>=51")
    return ids


def test_thread_explosion(
    client: httpx.Client, multi_thread_incident_id: str
) -> None:
    """Most important test this sprint.

    The real Sentinel export returns one row per thread entry, and keying on
    incident id alone would silently collapse conversation history.
    """
    response = client.post(
        "/v1/incidents/search",
        json={"incident_ids": [multi_thread_incident_id]},
    )
    assert response.status_code == 200
    payload = response.json()
    rows = payload["incidents"]
    assert payload["count"] == len(rows)
    assert len(rows) > 1, "expected more export rows than the one incident requested"

    ids = {row["id"] for row in rows}
    thread_ids = [row["threads.id"] for row in rows]
    assert ids == {multi_thread_incident_id}
    assert len(thread_ids) == len(set(thread_ids)), "threads.id must be distinct per row"


def test_fifty_id_cap(client: httpx.Client, sample_incident_ids: list[str]) -> None:
    ok = client.post(
        "/v1/incidents/search",
        json={"incident_ids": sample_incident_ids[:50]},
    )
    assert ok.status_code == 200

    too_many = client.post(
        "/v1/incidents/search",
        json={"incident_ids": sample_incident_ids[:51]},
    )
    assert too_many.status_code == 400
    body = too_many.text
    assert "max 50" in body or "50" in body


def test_rejects_keyless_query(client: httpx.Client) -> None:
    """Queries must be per-key; generic queries are not permitted."""
    empty = client.post("/v1/incidents/search", json={})
    assert empty.status_code == 400

    empty_list = client.post("/v1/incidents/search", json={"incident_ids": []})
    assert empty_list.status_code == 400


def test_field_names_match_real_export(
    client: httpx.Client, multi_thread_incident_id: str
) -> None:
    """Collector must see Flipkart export field names exactly.

    Only fetch() should change if we later switch from an API to a CSV download.
    """
    response = client.post(
        "/v1/incidents/search",
        json={"incident_ids": [multi_thread_incident_id]},
    )
    assert response.status_code == 200
    row = response.json()["incidents"][0]
    required = [
        "id",
        "issue.id",
        "issue.name",
        "orderId",
        "trackingId",
        "status.status",
        "status.statusType",
        "agingScore",
        "threads.id",
        "threads.threadEntryType.name",
        "threads.channel.name",
    ]
    missing = [key for key in required if key not in row]
    assert missing == [], f"missing export keys: {missing}"


def test_fault_injection(
    client: httpx.Client, multi_thread_incident_id: str
) -> None:
    ident = multi_thread_incident_id
    try:
        injected = client.post(f"/admin/fault/{ident}")
        assert injected.status_code == 200

        faulted = client.post(
            "/v1/incidents/search",
            json={"incident_ids": [ident]},
        )
        assert faulted.status_code == 500
        assert "injected fault" in faulted.text
    finally:
        cleared = client.delete("/admin/fault")
        assert cleared.status_code == 200

    recovered = client.post(
        "/v1/incidents/search",
        json={"incident_ids": [ident]},
    )
    assert recovered.status_code == 200


def test_data_properties(dsn: str) -> None:
    n_expected = int(os.environ.get("N_INCIDENTS", "1000"))
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sentinel_incident")
            (n_inc,) = cur.fetchone()
            cur.execute("SELECT count(*) FROM sentinel_thread")
            (n_thr,) = cur.fetchone()
            cur.execute(
                "SELECT count(*) FROM sentinel_incident WHERE tracking_id IS NULL"
            )
            (n_null,) = cur.fetchone()
            cur.execute(
                """
                SELECT left(tracking_id, 4) AS prefix
                FROM sentinel_incident
                WHERE tracking_id IS NOT NULL
                GROUP BY 1
                """
            )
            prefixes = {row[0] for row in cur.fetchall()}

    assert n_inc == n_expected
    assert n_thr > n_inc * 1.5
    null_share = n_null / n_inc if n_inc else 0.0
    assert 0.08 <= null_share <= 0.20
    assert {"FMPC", "FMPP", "FMPN"} <= prefixes
