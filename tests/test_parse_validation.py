"""parse() must reject malformed source shapes — never iterate a string as rows."""

from __future__ import annotations

import json

import pytest

from collector.contract import MalformedSourcePayload, Page, RawResponse
from collector.sources.sentinel import SentinelCollector
from collector.sources.sentinel_discovery import (
    CURSOR_PAGE_CAP,
    SentinelDiscoveryCollector,
)


def _raw(obj: object) -> RawResponse:
    if isinstance(obj, (bytes, bytearray)):
        body = bytes(obj)
    elif isinstance(obj, str):
        body = obj.encode("utf-8")
    else:
        body = json.dumps(obj).encode("utf-8")
    return RawResponse(body=body, content_type="application/json")


@pytest.fixture
def sentinel() -> SentinelCollector:
    return SentinelCollector()


@pytest.fixture
def discovery() -> SentinelDiscoveryCollector:
    return SentinelDiscoveryCollector()


@pytest.fixture
def enrich_page() -> Page:
    return Page(page_no=0, payload={"incident_ids": ["IN1"]})


@pytest.fixture
def discover_page() -> Page:
    return Page(
        page_no=0,
        payload={
            "updated_from": "2026-08-20T00:00:00Z",
            "updated_to": "2026-08-21T00:00:00Z",
            "limit": 1000,
        },
    )


def test_incidents_as_string_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    with pytest.raises(
        MalformedSourcePayload, match="incidents as str, expected list"
    ):
        sentinel.parse(
            _raw({"incidents": "not-a-list", "count": 11}), enrich_page
        )


def test_incidents_missing_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    with pytest.raises(MalformedSourcePayload, match="missing 'incidents'"):
        sentinel.parse(_raw({"count": 0}), enrich_page)


def test_incidents_as_dict_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    with pytest.raises(
        MalformedSourcePayload, match="incidents as dict, expected list"
    ):
        sentinel.parse(_raw({"incidents": {"id": "IN1"}}), enrich_page)


def test_element_is_string_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    with pytest.raises(
        MalformedSourcePayload, match=r"incidents\[0\] is str, expected dict"
    ):
        sentinel.parse(_raw({"incidents": ["IN1"]}), enrich_page)


def test_element_missing_id_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    with pytest.raises(
        MalformedSourcePayload, match=r"incidents\[0\] missing 'id'"
    ):
        sentinel.parse(
            _raw({"incidents": [{"issue.name": "Delay"}]}), enrich_page
        )


def test_body_is_bare_list_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    with pytest.raises(
        MalformedSourcePayload, match="returned list, expected dict"
    ):
        sentinel.parse(_raw([{"id": "IN1"}]), enrich_page)


def test_body_is_string_raises(
    sentinel: SentinelCollector, enrich_page: Page
) -> None:
    # json.loads of a JSON string value yields str
    with pytest.raises(
        MalformedSourcePayload, match="returned str, expected dict"
    ):
        sentinel.parse(_raw('"not-an-object"'), enrich_page)


def test_discovery_incident_ids_as_string_raises(
    discovery: SentinelDiscoveryCollector, discover_page: Page
) -> None:
    envelope = {
        "pages": [
            {
                "incident_ids": "IN1IN2",
                "count": 2,
                "has_more": False,
            }
        ],
        "partial": False,
        "cursor_pages_fetched": 1,
        "cursor_page_cap": CURSOR_PAGE_CAP,
        "filter": discover_page.payload,
    }
    with pytest.raises(
        MalformedSourcePayload, match="incident_ids as str, expected list"
    ):
        discovery.parse(_raw(envelope), discover_page)
