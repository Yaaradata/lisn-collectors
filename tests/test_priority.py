"""Request priority: queue order, flood guard, sweeper preservation."""

from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)

from collector.api import api  # noqa: E402
from collector.tasks import _run_sweep  # noqa: E402


def _patch_sinks():
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
        fetch,
        defer,
        cur,
    )


def test_priority_10_deferred_before_priority_0_order() -> None:
    """Capture defer configure calls: p10 must be configured with priority=10.

    Procrastinate fetch orders by priority DESC; we assert our wiring sets it.
    """
    client = TestClient(api)
    p_conn, p_app, p_fetch, fetch, defer, cur = _patch_sinks()

    with p_conn, p_app, p_fetch:
        r0 = client.post(
            "/v1/collect",
            json={
                "source": "sentinel",
                "query_spec": {"incident_ids": [f"IN{i:04d}" for i in range(50)]},
                "priority": 0,
            },
        )
        r10 = client.post(
            "/v1/collect",
            json={
                "source": "sentinel",
                "query_spec": {"incident_ids": ["INURGENT0001"]},
                "priority": 10,
            },
        )
    assert r0.status_code == 200, r0.text
    assert r10.status_code == 200, r10.text
    assert r10.json()["priority"] == 10

    # Last configure before each defer: collect kwargs
    configure_calls = fetch.configure.call_args_list
    priorities = [c.kwargs.get("priority", 0) for c in configure_calls]
    assert 10 in priorities
    assert priorities[-1] == 10  # urgent submitted last, still priority 10


def test_priority_6_with_50_pages_rejected() -> None:
    client = TestClient(api)
    # 50 pages × 50 keys = 2500 ids
    ids = [f"IN{i:06d}" for i in range(2500)]
    r = client.post(
        "/v1/collect",
        json={
            "source": "sentinel",
            "query_spec": {"incident_ids": ids},
            "priority": 6,
        },
    )
    assert r.status_code == 400, r.text
    assert "fast lane" in r.text.lower() or "20 pages" in r.text


def test_priority_out_of_range_rejected() -> None:
    client = TestClient(api)
    r = client.post(
        "/v1/collect",
        json={
            "source": "sentinel",
            "query_spec": {"incident_ids": ["IN1"]},
            "priority": 11,
        },
    )
    assert r.status_code == 400, r.text
    assert "priority" in r.text.lower()


@pytest.mark.asyncio
async def test_sweeper_preserves_priority_on_requeue() -> None:
    job_id = str(uuid.uuid4())
    conn = MagicMock()
    cur = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False

    fetchalls = [
        [(job_id, "sentinel", 7)],  # requeued with priority 7
        [],  # dead_rows
    ]
    cur.fetchall.side_effect = fetchalls
    cur.fetchone.return_value = None  # no existing procrastinate todo/doing

    async def _defer_async(*a, **k):
        return None

    defer_async = MagicMock(side_effect=_defer_async)
    configured = MagicMock()
    configured.defer_async = defer_async
    fetch = MagicMock()
    fetch.configure.return_value = configured

    async def _empty(*a, **k):
        return []

    async def _prune(*a, **k):
        return []

    app = MagicMock()
    app.job_manager.get_stalled_jobs = _empty
    app.job_manager.prune_stalled_workers = _prune

    with (
        patch("collector.tasks.connect", return_value=conn),
        patch("collector.tasks.fetch_page", fetch),
        patch("collector.tasks.app", app),
        patch("collector.tasks.mark_window_failed"),
    ):
        result = await _run_sweep()

    assert result["rows_requeued"] == 1
    fetch.configure.assert_called_with(queue="sentinel", priority=7)
    defer_async.assert_called_once()
