"""Unit tests for SentinelCollector.plan / parse (no live HTTP)."""

from __future__ import annotations

import json

import pytest

from collector.contract import Page, RawResponse
from collector.sources.sentinel import SentinelCollector


@pytest.fixture
def sentinel() -> SentinelCollector:
    return SentinelCollector()


def test_plan_1000_ids_makes_20_pages(sentinel: SentinelCollector) -> None:
    ids = [f"IN{i:026d}" for i in range(1000)]
    pages = sentinel.plan({"incident_ids": ids})
    assert len(pages) == 20
    assert [p.page_no for p in pages] == list(range(20))
    assert all(len(p.payload["incident_ids"]) == 50 for p in pages)
    assert pages[0].payload["incident_ids"][0] == ids[0]
    assert pages[-1].payload["incident_ids"][-1] == ids[-1]


def test_plan_page_boundaries(sentinel: SentinelCollector) -> None:
    assert len(sentinel.plan({"incident_ids": ["IN1"]})) == 1
    assert len(sentinel.plan({"incident_ids": [f"IN{i}" for i in range(50)]})) == 1
    pages = sentinel.plan({"incident_ids": [f"IN{i}" for i in range(51)]})
    assert len(pages) == 2
    assert pages[0].page_no == 0 and len(pages[0].payload["incident_ids"]) == 50
    assert pages[1].page_no == 1 and len(pages[1].payload["incident_ids"]) == 1


def test_plan_empty_raises(sentinel: SentinelCollector) -> None:
    with pytest.raises(
        ValueError,
        match="incident_ids, order_item_ids or order_ids required",
    ):
        sentinel.plan({})


def test_plan_status_only_raises(sentinel: SentinelCollector) -> None:
    with pytest.raises(
        ValueError,
        match="incident_ids, order_item_ids or order_ids required",
    ):
        sentinel.plan({"status": "open"})


def test_plan_order_ids(sentinel: SentinelCollector) -> None:
    pages = sentinel.plan({"order_ids": ["OD1", "OD2"]})
    assert len(pages) == 1
    assert pages[0].payload == {"order_ids": ["OD1", "OD2"]}
    assert "incident_ids" not in pages[0].payload


def test_plan_500_order_item_ids_makes_10_pages(sentinel: SentinelCollector) -> None:
    ids = [4_000_000_000_000_000 + i for i in range(500)]
    pages = sentinel.plan({"order_item_ids": ids})
    assert len(pages) == 10
    assert [p.page_no for p in pages] == list(range(10))
    assert all(len(p.payload["order_item_ids"]) == 50 for p in pages)
    assert pages[0].payload["order_item_ids"][0] == ids[0]
    assert pages[-1].payload["order_item_ids"][-1] == ids[-1]


def test_plan_order_item_ids_alone(sentinel: SentinelCollector) -> None:
    pages = sentinel.plan({"order_item_ids": [4_000_000_000_000_001]})
    assert len(pages) == 1
    assert pages[0].payload == {"order_item_ids": [4_000_000_000_000_001]}
    assert "incident_ids" not in pages[0].payload
    assert "order_ids" not in pages[0].payload


def test_plan_incident_and_order_item_ids_raises(sentinel: SentinelCollector) -> None:
    with pytest.raises(
        ValueError,
        match="exactly one of.*got incident_ids, order_item_ids",
    ):
        sentinel.plan(
            {
                "incident_ids": ["IN1"],
                "order_item_ids": [4_000_000_000_000_001],
            }
        )


def test_plan_empty_order_item_ids_raises(sentinel: SentinelCollector) -> None:
    with pytest.raises(
        ValueError,
        match="incident_ids, order_item_ids or order_ids required",
    ):
        sentinel.plan({"order_item_ids": []})


def test_parse_thread_explosion_keys(sentinel: SentinelCollector) -> None:
    fixture = {
        "incidents": [
            {
                "id": "IN1",
                "issue.name": "Delay in Delivery",
                "threads.id": "TH1",
                "threads.threadEntryType.name": "Note",
            },
            {
                "id": "IN1",
                "issue.name": "Delay in Delivery",
                "threads.id": "TH2",
                "threads.threadEntryType.name": "Email",
            },
            {
                "id": "IN1",
                "issue.name": "Delay in Delivery",
                "threads.id": "TH3",
                "threads.threadEntryType.name": "Outbound",
            },
        ],
        "count": 3,
    }
    raw = RawResponse(
        body=json.dumps(fixture).encode("utf-8"),
        content_type="application/json",
    )
    page = Page(page_no=0, payload={"incident_ids": ["IN1"]})
    records = sentinel.parse(raw, page)
    assert len(records) == 3
    keys = [r.key for r in records]
    assert keys == ["IN1::TH1", "IN1::TH2", "IN1::TH3"]
    assert len(set(keys)) == 3
    assert all(k.startswith("IN1::") for k in keys)
    assert records[0].data["issue_name"] == "Delay in Delivery"
    assert records[0].data["threads_threadEntryType_name"] == "Note"
