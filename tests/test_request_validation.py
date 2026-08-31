"""API request validation for /v1/collect query_spec shapes."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# collector.app reads COLLECTOR_DSN at import time.
os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)

from collector.api import api  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(api)


def test_incident_ids_bare_string_400(client: TestClient) -> None:
    r = client.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": "IN2608"}},
    )
    assert r.status_code == 400, r.text
    assert "incident_ids" in r.text


def test_incident_ids_list_of_ints_400(client: TestClient) -> None:
    r = client.post(
        "/v1/collect",
        json={"source": "sentinel", "query_spec": {"incident_ids": [1, 2, 3]}},
    )
    assert r.status_code == 400, r.text
    assert "incident_ids" in r.text


def test_two_key_types_400_names_both(client: TestClient) -> None:
    r = client.post(
        "/v1/collect",
        json={
            "source": "sentinel",
            "query_spec": {
                "incident_ids": ["IN1"],
                "order_ids": ["OD1"],
            },
        },
    )
    assert r.status_code == 400, r.text
    body = r.text
    assert "incident_ids" in body
    assert "order_ids" in body


def test_unknown_field_in_query_spec_400(client: TestClient) -> None:
    r = client.post(
        "/v1/collect",
        json={
            "source": "sentinel",
            "query_spec": {"incident_ids": ["IN1"], "status": "open"},
        },
    )
    assert r.status_code == 400, r.text
    assert "status" in r.text


def test_order_item_ids_on_discovery_400(client: TestClient) -> None:
    r = client.post(
        "/v1/collect",
        json={
            "source": "sentinel_discovery",
            "query_spec": {
                "updated_from": "2026-08-20T00:00:00Z",
                "updated_to": "2026-08-21T00:00:00Z",
                "order_item_ids": ["OI1"],
            },
        },
    )
    assert r.status_code == 400, r.text
    assert "order_item_ids" in r.text


def _patch_collect_sinks():
    """Avoid Cloud SQL / Procrastinate on valid-shape 200 tests."""
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    app_cm = MagicMock()
    app_cm.__enter__.return_value = None
    app_cm.__exit__.return_value = False

    defer = MagicMock()
    configured = MagicMock()
    configured.defer = defer
    fetch = MagicMock()
    fetch.configure.return_value = configured

    return (
        patch("collector.api.connect", return_value=conn),
        patch("collector.api.procrastinate_app.open", return_value=app_cm),
        patch("collector.api.fetch_page", fetch),
    )


def test_valid_sentinel_key_query_200(client: TestClient) -> None:
    p_connect, p_app, p_fetch = _patch_collect_sinks()
    with p_connect, p_app, p_fetch:
        r = client.post(
            "/v1/collect",
            json={
                "source": "sentinel",
                "query_spec": {"incident_ids": ["IN26081800000000000001"]},
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_pages"] == 1
    assert body["keys"] == 1
    assert "request_id" in body


def test_valid_discovery_query_200(client: TestClient) -> None:
    p_connect, p_app, p_fetch = _patch_collect_sinks()
    with p_connect, p_app, p_fetch:
        r = client.post(
            "/v1/collect",
            json={
                "source": "sentinel_discovery",
                "query_spec": {
                    "updated_from": "2026-08-20T00:00:00Z",
                    "updated_to": "2026-08-21T00:00:00Z",
                    "limit": 100,
                },
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_pages"] == 1
    assert "request_id" in body
