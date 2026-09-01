"""Unit tests for partial discovery window finalization (no Cloud SQL)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from collector.discovery_gaps import effective_discovery_limit, maybe_finalize_window

EFFECTIVE_CAP = 10000


def _mock_conn(cur: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.commit = MagicMock()
    return conn


def test_finalize_marks_partial_when_id_count_hits_cap() -> None:
    request_id = "00000000-0000-4000-8000-000000000001"
    query_spec = {
        "updated_from": "2026-08-20T00:00:00Z",
        "updated_to": "2026-08-27T00:00:00Z",
        "limit": 1000,
    }

    cur = MagicMock()
    cur.fetchone.return_value = (1, 1, 0, EFFECTIVE_CAP)
    cur.rowcount = 1
    conn = _mock_conn(cur)

    with patch("collector.discovery_gaps.connect", return_value=conn):
        with patch(
            "collector.discovery_gaps._load_query_spec",
            return_value=query_spec,
        ):
            status = maybe_finalize_window(request_id)

    assert status == "partial"
    update_sql = cur.execute.call_args_list[-1][0][0]
    assert "SET status = %s" in update_sql
    assert cur.execute.call_args_list[-1][0][1][0] == "partial"


def test_finalize_stays_complete_below_cap() -> None:
    request_id = "00000000-0000-4000-8000-000000000002"
    query_spec = {"limit": 1000}

    cur = MagicMock()
    cur.fetchone.return_value = (1, 1, 0, 445)
    cur.rowcount = 1
    conn = _mock_conn(cur)

    with patch("collector.discovery_gaps.connect", return_value=conn):
        with patch(
            "collector.discovery_gaps._load_query_spec",
            return_value=query_spec,
        ):
            status = maybe_finalize_window(request_id)

    assert status == "complete"
    assert cur.execute.call_args_list[-1][0][1][0] == "complete"


def test_effective_limit_matches_production_defaults() -> None:
    assert effective_discovery_limit({"limit": 1000}) == EFFECTIVE_CAP
