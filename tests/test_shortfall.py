"""Shortfall accounting: requested vs returned distinct source entities."""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault(
    "COLLECTOR_DSN", "postgresql://unused:unused@127.0.0.1:5432/collector"
)

from collector.contract import Page, Record
from collector.shortfall import (
    requested_count,
    returned_count,
    shortfall_keys,
)


def _records_for_incidents(ids: list[str], threads: int = 2) -> list[Record]:
    out: list[Record] = []
    for iid in ids:
        for t in range(threads):
            out.append(
                Record(
                    key=f"{iid}::TH{t}",
                    data={"id": iid, "orderId": f"OD-{iid}", "orderItemId": f"OI-{iid}"},
                )
            )
    return out


def test_returned_count_is_distinct_entities_not_rows() -> None:
    records = _records_for_incidents([f"IN{i}" for i in range(50)], threads=3)
    assert len(records) == 150
    assert returned_count(records, {"incident_ids": [f"IN{i}" for i in range(50)]}) == 50


def test_all_keys_exist_missing_zero() -> None:
    keys = [f"IN{i:04d}" for i in range(50)]
    page = Page(page_no=0, payload={"incident_ids": keys})
    records = _records_for_incidents(keys, threads=2)
    assert requested_count(page.payload) == 50
    assert returned_count(records, page.payload) == 50
    delta = shortfall_keys(page, records)
    assert delta is not None
    assert delta["missing_total"] == 0
    assert delta["unexpected_total"] == 0


def test_ten_missing_keys_still_done_shape() -> None:
    real = [f"IN{i:04d}" for i in range(40)]
    fake = [f"FAKE{i:04d}" for i in range(10)]
    keys = real + fake
    page = Page(page_no=0, payload={"incident_ids": keys})
    records = _records_for_incidents(real, threads=2)
    assert requested_count(page.payload) == 50
    assert returned_count(records, page.payload) == 40
    delta = shortfall_keys(page, records)
    assert delta is not None
    assert delta["missing_total"] == 10
    assert set(delta["missing"]) == set(fake)
    assert delta["unexpected_total"] == 0


def test_missing_keys_sample_capped_at_50() -> None:
    keys = [f"MISS{i:04d}" for i in range(60)]
    page = Page(page_no=0, payload={"incident_ids": keys})
    delta = shortfall_keys(page, [])
    assert delta is not None
    assert delta["missing_total"] == 60
    assert len(delta["missing"]) == 50


def test_unexpected_keys_recorded() -> None:
    """Pass 4 / protocol D-9: returned-but-not-requested is an anomaly."""
    keys = [f"IN{i:04d}" for i in range(3)]
    page = Page(page_no=0, payload={"incident_ids": keys})
    records = _records_for_incidents(keys + ["IN_UNREQUESTED_EXTRA_0001"], threads=1)
    delta = shortfall_keys(page, records)
    assert delta is not None
    assert delta["missing_total"] == 0
    assert delta["unexpected_total"] == 1
    assert delta["unexpected"] == ["IN_UNREQUESTED_EXTRA_0001"]


def test_counts_endpoint_sums_across_20_pages() -> None:
    """Simulate 20 done pages via SQL aggregates the endpoint uses."""
    from collector.api import api

    def fake_connect():
        conn = MagicMock()
        cur = MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        conn.cursor.return_value.__exit__.return_value = False
        calls: list[str] = []

        def execute(sql, params=None):
            calls.append(" ".join(sql.split()))
            if len(calls) == 1:
                cur.fetchall.return_value = [("done", 20)]
            else:
                # 20 pages × 50 requested, 45 returned, 90 BQ rows
                cur.fetchone.return_value = (1800, 1000, 900)

        cur.execute.side_effect = execute
        return conn

    client = TestClient(api)
    rid = str(uuid.uuid4())
    with patch("collector.api.connect", side_effect=fake_connect):
        r = client.get(f"/v1/requests/{rid}/counts")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"] == {"done": 20}
    assert body["requested"] == 1000
    assert body["returned"] == 900
    assert body["missing"] == 100
    assert body["records"] == 1800
