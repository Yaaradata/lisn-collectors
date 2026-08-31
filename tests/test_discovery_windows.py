"""Discovery window ledger — record / complete / fail lifecycle."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)

from collector.api import api
from collector.discovery_gaps import (
    maybe_finalize_window,
    reconcile_running_windows,
)
from tests._sql_apply import apply_sql_file

SOURCE = "sentinel_discovery"
T0 = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)


def _load_dotenv() -> None:
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.isfile(env_path):
        return
    for raw in open(env_path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


class _CursorCtx:
    def __init__(self, cur):
        self._cur = cur

    def __enter__(self):
        return self._cur

    def __exit__(self, *args):
        return False

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _ConnProxy:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _CursorCtx(self._conn.cursor())

    def commit(self):
        self._conn.commit()


@pytest.fixture
def db():
    try:
        from google.cloud.sql.connector import Connector
    except ImportError:
        pytest.skip("cloud-sql-python-connector not installed")

    _load_dotenv()
    conn_name = os.environ.get("CONN")
    dbpw = os.environ.get("DBPW")
    if not conn_name or not dbpw:
        pytest.skip("CONN/DBPW not set")

    connector = Connector()
    conn = connector.connect(
        conn_name, "pg8000", user="postgres", password=dbpw, db="collector"
    )
    cur = conn.cursor()
    schema = os.path.join(
        os.path.dirname(__file__), "..", "sql", "010_discovery_window.sql"
    )
    apply_sql_file(cur, schema)
    conn.commit()
    # Wipe this source's ledger so contiguity / finalize tests are isolated
    # on the shared Cloud SQL instance.
    cur.execute(
        "DELETE FROM discovery_window WHERE source = %s",
        (SOURCE,),
    )
    conn.commit()
    yield conn, connector, _ConnProxy(conn)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM discovery_window WHERE source = %s",
        (SOURCE,),
    )
    conn.commit()
    conn.close()
    connector.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(api)


def test_window_from_ge_window_to_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/collect",
        json={
            "source": SOURCE,
            "query_spec": {
                "updated_from": "2026-08-21T10:00:00Z",
                "updated_to": "2026-08-21T10:00:00Z",
            },
        },
    )
    assert r.status_code == 400, r.text
    assert "strictly before" in r.text.lower() or "updated_from" in r.text.lower()


def test_discovery_creates_running_row(db, client: TestClient) -> None:
    conn, _, proxy = db
    start, end = T0, T0 + timedelta(hours=1)
    defer = MagicMock()
    configured = MagicMock()
    configured.defer = defer
    fetch = MagicMock()
    fetch.configure.return_value = configured
    app_cm = MagicMock()
    app_cm.__enter__.return_value = None
    app_cm.__exit__.return_value = False

    page = MagicMock()
    page.page_no = 0
    page.payload = {
        "updated_from": start.isoformat(),
        "updated_to": end.isoformat(),
    }

    with (
        patch("collector.api.connect", return_value=proxy),
        patch("collector.discovery_gaps.connect", return_value=proxy),
        patch("collector.api.procrastinate_app.open", return_value=app_cm),
        patch("collector.api.fetch_page", fetch),
        patch("collector.api.get") as get_src,
    ):
        get_src.return_value.plan.return_value = [page]
        r = client.post(
            "/v1/collect",
            json={
                "source": SOURCE,
                "query_spec": {
                    "updated_from": start.isoformat().replace("+00:00", "Z"),
                    "updated_to": end.isoformat().replace("+00:00", "Z"),
                },
                "allow_gap": True,
                "gap_reason": "test: isolated running-row assertion",
            },
        )
    assert r.status_code == 200, r.text
    rid = r.json()["request_id"]
    cur = conn.cursor()
    cur.execute(
        """
        SELECT status, window_field, id_count, window_from, window_to
        FROM discovery_window WHERE request_id = %s
        """,
        (rid,),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "running"
    assert row[1] == "updated_on"
    assert row[2] is None


def test_window_reaches_complete_with_id_count(db) -> None:
    conn, _, proxy = db
    rid = uuid.uuid4()
    jid = uuid.uuid4()
    start, end = T0 + timedelta(hours=2), T0 + timedelta(hours=3)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO collector_request (
          request_id, source, query_spec, total_pages, status
        ) VALUES (%s, %s, '{}'::jsonb, 1, 'open')
        """,
        (rid, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO collector_job (
          job_id, request_id, source, page_no, page_payload, status,
          record_count
        ) VALUES (%s, %s, %s, 0, '{}'::jsonb, 'done', 42)
        """,
        (jid, rid, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO discovery_window (
          source, window_field, window_from, window_to, request_id, status
        ) VALUES (%s, 'updated_on', %s, %s, %s, 'running')
        """,
        (SOURCE, start, end, rid),
    )
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        status = maybe_finalize_window(str(rid))
    assert status == "complete"
    cur = conn.cursor()
    cur.execute(
        """
        SELECT status, id_count, completed_at IS NOT NULL
        FROM discovery_window WHERE request_id = %s
        """,
        (rid,),
    )
    row = cur.fetchone()
    assert list(row) == ["complete", 42, True]


def test_failed_discovery_reaches_failed(db) -> None:
    conn, _, proxy = db
    rid = uuid.uuid4()
    jid = uuid.uuid4()
    start, end = T0 + timedelta(hours=4), T0 + timedelta(hours=5)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO collector_request (
          request_id, source, query_spec, total_pages, status
        ) VALUES (%s, %s, '{}'::jsonb, 1, 'open')
        """,
        (rid, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO collector_job (
          job_id, request_id, source, page_no, page_payload, status,
          record_count
        ) VALUES (%s, %s, %s, 0, '{}'::jsonb, 'dead', 0)
        """,
        (jid, rid, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO discovery_window (
          source, window_field, window_from, window_to, request_id, status
        ) VALUES (%s, 'updated_on', %s, %s, %s, 'running')
        """,
        (SOURCE, start, end, rid),
    )
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        status = maybe_finalize_window(str(rid))
    assert status == "failed"
    cur = conn.cursor()
    cur.execute(
        "SELECT status FROM discovery_window WHERE request_id = %s",
        (rid,),
    )
    assert cur.fetchone()[0] == "failed"


def test_sweeper_backstop_finalises_running_window(db) -> None:
    """(b) Sweeper reconciles a window stuck at running after pages finished."""
    conn, _, proxy = db
    rid = uuid.uuid4()
    jid = uuid.uuid4()
    start, end = T0 + timedelta(hours=6), T0 + timedelta(hours=7)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO collector_request (
          request_id, source, query_spec, total_pages, status
        ) VALUES (%s, %s, '{}'::jsonb, 1, 'open')
        """,
        (rid, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO collector_job (
          job_id, request_id, source, page_no, page_payload, status,
          record_count
        ) VALUES (%s, %s, %s, 0, '{}'::jsonb, 'done', 7)
        """,
        (jid, rid, SOURCE),
    )
    cur.execute(
        """
        INSERT INTO discovery_window (
          source, window_field, window_from, window_to, request_id, status
        ) VALUES (%s, 'updated_on', %s, %s, %s, 'running')
        """,
        (SOURCE, start, end, rid),
    )
    conn.commit()

    with patch("collector.discovery_gaps.connect", return_value=proxy):
        n = reconcile_running_windows()
    assert n >= 1
    cur = conn.cursor()
    cur.execute(
        """
        SELECT status, id_count FROM discovery_window
        WHERE request_id = %s
        """,
        (rid,),
    )
    assert list(cur.fetchone()) == ["complete", 7]
