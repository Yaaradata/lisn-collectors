"""Unit tests for SentinelDiscoveryCollector (plan / parse / fetch cursor cap)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from collector.contract import Page, RawResponse
from collector.sources.sentinel_discovery import (
    CURSOR_PAGE_CAP,
    SentinelDiscoveryCollector,
)


@pytest.fixture
def discovery() -> SentinelDiscoveryCollector:
    return SentinelDiscoveryCollector()


def _valid_filter(**extra: object) -> dict:
    body: dict = {
        "updated_from": "2026-08-20T00:00:00Z",
        "updated_to": "2026-08-21T00:00:00Z",
        "limit": 1000,
    }
    body.update(extra)
    return body


def test_plan_no_time_window_raises(discovery: SentinelDiscoveryCollector) -> None:
    with pytest.raises(ValueError, match="time window"):
        discovery.plan({"statuses": ["Unresolved"]})


def test_plan_16_day_window_raises(discovery: SentinelDiscoveryCollector) -> None:
    with pytest.raises(ValueError, match="15-day"):
        discovery.plan(
            {
                "updated_from": "2026-08-01T00:00:00Z",
                "updated_to": "2026-08-17T00:00:00Z",
            }
        )


def test_plan_valid_filter_produces_one_page(
    discovery: SentinelDiscoveryCollector,
) -> None:
    pages = discovery.plan(_valid_filter(statuses=["Unresolved"]))
    assert len(pages) == 1
    assert pages[0].page_no == 0
    assert pages[0].payload["updated_from"] == "2026-08-20T00:00:00Z"
    assert pages[0].payload["statuses"] == ["Unresolved"]
    assert "cursor" not in pages[0].payload


def test_parse_fixture_one_record_per_id(
    discovery: SentinelDiscoveryCollector,
) -> None:
    page = Page(page_no=0, payload=_valid_filter())
    envelope = {
        "pages": [
            {
                "incident_ids": ["IN1", "IN2", "IN3"],
                "count": 3,
                "next_cursor": None,
                "has_more": False,
            }
        ],
        "partial": False,
        "cursor_pages_fetched": 1,
        "cursor_page_cap": CURSOR_PAGE_CAP,
        "filter": page.payload,
    }
    raw = RawResponse(
        body=json.dumps(envelope).encode("utf-8"),
        content_type="application/json",
    )
    records = discovery.parse(raw, page)
    assert len(records) == 3
    assert [r.key for r in records] == ["IN1", "IN2", "IN3"]
    assert all(r.data["incident_id"] == r.key for r in records)
    assert all(r.data["cursor_page"] == 0 for r in records)
    assert all(r.data["filter_hash"] for r in records)
    assert all(r.data["discovered_at"] for r in records)
    assert all(r.data["partial"] is False for r in records)


def test_fetch_cursor_loop_stops_at_cap_and_marks_partial(
    discovery: SentinelDiscoveryCollector,
) -> None:
    """Option (b): internal cursor follow hits CURSOR_PAGE_CAP → partial."""
    page = Page(page_no=0, payload=_valid_filter(limit=2))

    def _fake_json(i: int) -> dict:
        return {
            "incident_ids": [f"IN{i}a", f"IN{i}b"],
            "count": 2,
            "next_cursor": f"IN{i}b",
            "has_more": True,  # never clears — forces the cap
        }

    call_count = {"n": 0}

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._payload = payload
            self.content = json.dumps(payload).encode("utf-8")
            self.headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: dict | None = None, timeout: float = 0) -> _Resp:
            del url, timeout
            i = call_count["n"]
            call_count["n"] += 1
            assert json is not None
            if i == 0:
                assert "cursor" not in json
            else:
                assert json.get("cursor") == f"IN{i - 1}b"
            return _Resp(_fake_json(i))

    with patch.dict("os.environ", {"SENTINEL_URL": "http://mock.test"}):
        with patch(
            "collector.sources.sentinel_discovery.get_client",
            return_value=_Client(),
        ):
            with patch("collector.sources.sentinel_discovery.time.sleep") as sleep:
                raw = discovery.fetch(page)

    assert call_count["n"] == CURSOR_PAGE_CAP
    assert sleep.call_count == CURSOR_PAGE_CAP - 1
    for args, _kwargs in sleep.call_args_list:
        assert args[0] == discovery.min_interval_s

    envelope = json.loads(raw.body.decode("utf-8"))
    assert envelope["partial"] is True
    assert envelope["cursor_pages_fetched"] == CURSOR_PAGE_CAP
    assert envelope["cursor_page_cap"] == CURSOR_PAGE_CAP
    assert len(envelope["pages"]) == CURSOR_PAGE_CAP

    records = discovery.parse(raw, page)
    assert len(records) == CURSOR_PAGE_CAP * 2
    assert all(r.data["partial"] is True for r in records)
