"""Sprint 2 exit criteria: pytest against a running mock at :8081."""

from __future__ import annotations

import os
from datetime import timedelta
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

    empty_order_items = client.post(
        "/v1/incidents/search", json={"order_item_ids": []}
    )
    assert empty_order_items.status_code == 400


def test_order_item_id_matches_incident_id_query(
    client: httpx.Client, dsn: str
) -> None:
    """Same incident + threads whether keyed by incident id or order item id."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.order_item_id
                FROM sentinel_incident AS i
                JOIN sentinel_thread AS t ON t.incident_id = i.id
                WHERE i.order_item_id IS NOT NULL
                GROUP BY i.id, i.order_item_id
                HAVING count(t.thread_id) > 1
                ORDER BY i.id
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        pytest.fail(
            "Need a multi-thread incident with order_item_id — run `make seed`"
        )
    incident_id, order_item_id = row
    order_item_id_str = str(order_item_id)

    by_incident = client.post(
        "/v1/incidents/search",
        json={"incident_ids": [incident_id]},
    )
    by_order_item = client.post(
        "/v1/incidents/search",
        json={"order_item_ids": [order_item_id_str]},
    )
    assert by_incident.status_code == 200
    assert by_order_item.status_code == 200

    left = by_incident.json()["incidents"]
    right = by_order_item.json()["incidents"]
    assert left == right
    assert len(left) > 1
    assert {row["id"] for row in left} == {incident_id}
    assert all(row["orderItemId"] == order_item_id_str for row in left)


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
    # N_INCIDENTS override → exact count. Date-range seed → any non-empty set.
    n_raw = os.environ.get("N_INCIDENTS", "").strip()
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

    if n_raw:
        assert n_inc == int(n_raw)
    else:
        assert n_inc > 0
    assert n_thr > n_inc * 1.5
    null_share = n_null / n_inc if n_inc else 0.0
    assert 0.08 <= null_share <= 0.20
    assert {"FMPC", "FMPP", "FMPN"} <= prefixes


# ---------------------------------------------------------------------------
# Discovery — console-shaped "which ids" before per-key enrichment
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def discover_updated_window(dsn: str) -> dict[str, str]:
    """A ≤15-day updated window that covers the seeded incident set."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT min(updated_on), max(updated_on) FROM sentinel_incident"
            )
            lo, hi = cur.fetchone()
    if lo is None or hi is None:
        pytest.fail("sentinel_incident is empty — run `make seed`")
    start = lo - timedelta(hours=1)
    end = hi + timedelta(hours=1)
    assert (end - start).days <= 15
    return {
        "updated_from": start.isoformat(),
        "updated_to": end.isoformat(),
    }


def test_discover_requires_time_window(client: httpx.Client) -> None:
    response = client.post("/v1/incidents/discover", json={})
    assert response.status_code == 400
    assert "time window" in response.text.lower() or "window" in response.text.lower()

    statuses_only = client.post(
        "/v1/incidents/discover",
        json={"statuses": ["Unresolved"]},
    )
    assert statuses_only.status_code == 400


def test_discover_rejects_16_day_window(client: httpx.Client) -> None:
    response = client.post(
        "/v1/incidents/discover",
        json={
            "updated_from": "2026-08-01T00:00:00Z",
            "updated_to": "2026-08-17T00:00:00Z",  # 16 days
        },
    )
    assert response.status_code == 400
    assert "15" in response.text


def test_discover_accepts_15_day_window(client: httpx.Client) -> None:
    response = client.post(
        "/v1/incidents/discover",
        json={
            "updated_from": "2026-08-01T00:00:00Z",
            "updated_to": "2026-08-16T00:00:00Z",  # exactly 15 days
            "limit": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert "incident_ids" in body
    assert body["count"] == len(body["incident_ids"])
    assert "has_more" in body
    assert "next_cursor" in body
    # Discovery returns ids only — no incident bodies.
    assert "incidents" not in body


def test_discover_status_filter_narrows(
    client: httpx.Client, discover_updated_window: dict[str, str], dsn: str
) -> None:
    all_resp = client.post(
        "/v1/incidents/discover",
        json={**discover_updated_window, "limit": 5000},
    )
    assert all_resp.status_code == 200
    all_ids = set(all_resp.json()["incident_ids"])

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status_status, count(*) AS n
                FROM sentinel_incident
                GROUP BY 1
                ORDER BY n DESC
                LIMIT 1
                """
            )
            status, _ = cur.fetchone()

    filtered = client.post(
        "/v1/incidents/discover",
        json={**discover_updated_window, "statuses": [status], "limit": 5000},
    )
    assert filtered.status_code == 200
    filtered_ids = set(filtered.json()["incident_ids"])
    assert filtered_ids < all_ids
    assert filtered_ids

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sentinel_incident WHERE status_status = %s",
                (status,),
            )
            expected = {r[0] for r in cur.fetchall()}
    assert filtered_ids == expected & all_ids


def test_discover_issue_names_filter_narrows(
    client: httpx.Client, discover_updated_window: dict[str, str], dsn: str
) -> None:
    all_resp = client.post(
        "/v1/incidents/discover",
        json={**discover_updated_window, "limit": 5000},
    )
    assert all_resp.status_code == 200
    all_ids = set(all_resp.json()["incident_ids"])

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT issue_name, count(*) AS n
                FROM sentinel_incident
                GROUP BY 1
                ORDER BY n DESC
                LIMIT 1
                """
            )
            issue_name, _ = cur.fetchone()

    filtered = client.post(
        "/v1/incidents/discover",
        json={
            **discover_updated_window,
            "issue_names": [issue_name],
            "limit": 5000,
        },
    )
    assert filtered.status_code == 200
    filtered_ids = set(filtered.json()["incident_ids"])
    assert filtered_ids < all_ids
    assert filtered_ids


def test_discover_cursor_pages_are_disjoint_and_complete(
    client: httpx.Client, discover_updated_window: dict[str, str]
) -> None:
    full = client.post(
        "/v1/incidents/discover",
        json={**discover_updated_window, "limit": 5000},
    )
    assert full.status_code == 200
    full_body = full.json()
    assert full_body["has_more"] is False
    expected = full_body["incident_ids"]
    assert len(expected) >= 2

    page_size = max(1, len(expected) // 3)
    collected: list[str] = []
    cursor = None
    for _ in range(50):
        payload: dict = {
            **discover_updated_window,
            "limit": page_size,
        }
        if cursor is not None:
            payload["cursor"] = cursor
        page = client.post("/v1/incidents/discover", json=payload)
        assert page.status_code == 200
        body = page.json()
        chunk = body["incident_ids"]
        assert not (set(chunk) & set(collected)), "pages must be disjoint"
        collected.extend(chunk)
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        assert body["next_cursor"] == chunk[-1]
        cursor = body["next_cursor"]
    else:
        pytest.fail("paging did not terminate")

    assert collected == expected
