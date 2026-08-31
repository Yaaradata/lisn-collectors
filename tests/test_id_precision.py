"""Boundary probes for orderItemId / orderItemUnitId / threads.communicationId.

Compare PARSED VALUES, never rendered text — "4000000000299190.0" and
"4.00000000029919E15" are the same float written differently and must not
drive a false alarm. After the STRING fix, parsed values are Python strs
(or None).
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from collector.contract import MalformedSourcePayload, Page, RawResponse
from collector.sources.sentinel import SentinelCollector
from mock.sentinel_api import _id_string as mock_id_string
from mock.sentinel_api import _row_to_export

# Just below 2^53 — must survive even under float (control).
PROBE_BELOW = "9007199254740991"
# Just above 2^53 — previously arrived off by 1 as float.
PROBE_ABOVE = "9007199254740993"
# 19-digit — previously arrived off by 11 as float.
PROBE_WIDE = "1234567890123456789"
# Leading zero — proves strings are not renumbered.
PROBE_LEADING_ZERO = "0123456789012345678"


def test_mock_id_string_null_stays_none() -> None:
    """Genuine NULL must serialise as None, not 0 / "0" (the old `or 0` bug)."""
    assert mock_id_string(None) is None


def test_mock_id_string_preserves_boundary_and_leading_zero() -> None:
    assert mock_id_string(PROBE_BELOW) == PROBE_BELOW
    assert mock_id_string(PROBE_ABOVE) == PROBE_ABOVE
    assert mock_id_string(PROBE_WIDE) == PROBE_WIDE
    assert mock_id_string(PROBE_LEADING_ZERO) == PROBE_LEADING_ZERO
    # Decimal from a numeric column must not go through float.
    assert mock_id_string(Decimal(PROBE_ABOVE)) == PROBE_ABOVE
    assert mock_id_string(Decimal(PROBE_WIDE)) == PROBE_WIDE


def test_mock_row_to_export_emits_strings_not_floats() -> None:
    row = {
        "id": "IN-PROBE",
        "issue_id": 1,
        "issue_name": "x",
        "issue_parent_id": None,
        "issue_parent_name": None,
        "order_id": "OD1",
        "order_item_id": Decimal(PROBE_ABOVE),
        "order_item_unit_id": None,  # NULL → None, not 0
        "tracking_id": None,
        "order_item_product_fsn": "FSN",
        "incident_score": 1,
        "resolution_deadline": None,
        "resolution_deadline_breach": None,
        "seller_id": "S1",
        "source": "Sentinel",
        "status_id": 1,
        "status_status": "Unresolved",
        "status_status_type": "UNRESOLVED",
        "subject": "s",
        "updated_on": None,
        "aging_score": 1,
        "last_updated_by_user": "u",
        "queue": "q",
        "assigned_to": "a",
        "thread_id": "TH1",
        "channel_id": 5,
        "channel_name": "Outbound",
        "communication_id": Decimal(PROBE_WIDE),
        "content_type": "text/plain",
        "thread_created_at": None,
        "created_by": "u",
        "system_thread": True,
        "thread_entry_type_id": 1,
        "thread_entry_type_name": "Note",
        "updated_by": "u",
    }
    exported = _row_to_export(row)
    assert exported["orderItemId"] == PROBE_ABOVE
    assert isinstance(exported["orderItemId"], str)
    assert exported["orderItemUnitId"] is None
    assert exported["threads.communicationId"] == PROBE_WIDE
    # Wire JSON must quote them as strings, not numbers.
    wire = json.dumps({"incidents": [exported]})
    parsed = json.loads(wire)["incidents"][0]
    assert parsed["orderItemId"] == PROBE_ABOVE
    assert parsed["orderItemUnitId"] is None
    assert parsed["threads.communicationId"] == PROBE_WIDE
    assert isinstance(parsed["orderItemId"], str)


def test_parse_preserves_boundary_probes_as_strings() -> None:
    src = SentinelCollector()
    incidents = [
        {
            "id": "IN-BELOW",
            "orderItemId": PROBE_BELOW,
            "orderItemUnitId": PROBE_BELOW,
            "threads.id": "TH1",
            "threads.communicationId": PROBE_BELOW,
        },
        {
            "id": "IN-ABOVE",
            "orderItemId": PROBE_ABOVE,
            "orderItemUnitId": PROBE_ABOVE,
            "threads.id": "TH2",
            "threads.communicationId": PROBE_ABOVE,
        },
        {
            "id": "IN-WIDE",
            "orderItemId": PROBE_WIDE,
            "orderItemUnitId": PROBE_WIDE,
            "threads.id": "TH3",
            "threads.communicationId": PROBE_WIDE,
        },
        {
            "id": "IN-ZERO",
            "orderItemId": PROBE_LEADING_ZERO,
            "orderItemUnitId": PROBE_LEADING_ZERO,
            "threads.id": "TH4",
            "threads.communicationId": PROBE_LEADING_ZERO,
        },
    ]
    raw = RawResponse(
        body=json.dumps({"incidents": incidents}).encode("utf-8"),
        content_type="application/json",
    )
    records = src.parse(raw, Page(page_no=0, payload={"incident_ids": ["x"]}))
    by_id = {r.data["id"]: r.data for r in records}

    assert by_id["IN-BELOW"]["orderItemId"] == PROBE_BELOW
    assert by_id["IN-ABOVE"]["orderItemId"] == PROBE_ABOVE
    assert by_id["IN-WIDE"]["orderItemId"] == PROBE_WIDE
    assert by_id["IN-ZERO"]["orderItemId"] == PROBE_LEADING_ZERO
    # Parsed values — type and equality, not rendered text forms of a float.
    for data in by_id.values():
        assert isinstance(data["orderItemId"], str)
        assert isinstance(data["orderItemUnitId"], str)
        assert isinstance(data["threads_communicationId"], str)


def test_parse_rejects_float_identifiers() -> None:
    src = SentinelCollector()
    raw = RawResponse(
        body=json.dumps(
            {
                "incidents": [
                    {
                        "id": "IN-BAD",
                        "orderItemId": float(PROBE_ABOVE),  # already rounded
                        "threads.id": "TH1",
                    }
                ]
            }
        ).encode("utf-8"),
        content_type="application/json",
    )
    with pytest.raises(MalformedSourcePayload, match="float"):
        src.parse(raw, Page(page_no=0, payload={"incident_ids": ["IN-BAD"]}))


def test_parse_null_id_stays_none() -> None:
    src = SentinelCollector()
    raw = RawResponse(
        body=json.dumps(
            {
                "incidents": [
                    {
                        "id": "IN-NULL",
                        "orderItemId": None,
                        "orderItemUnitId": None,
                        "threads.id": "TH1",
                        "threads.communicationId": None,
                    }
                ]
            }
        ).encode("utf-8"),
        content_type="application/json",
    )
    rec = src.parse(raw, Page(page_no=0, payload={"incident_ids": ["IN-NULL"]}))[0]
    assert rec.data["orderItemId"] is None
    assert rec.data["orderItemUnitId"] is None
    assert rec.data["threads_communicationId"] is None


def test_float_still_mutates_above_2_53_control() -> None:
    """Document the bug we fixed: float() alone is enough to corrupt."""
    assert str(int(float(PROBE_BELOW))) == PROBE_BELOW  # exact below 2^53
    assert str(int(float(PROBE_ABOVE))) != PROBE_ABOVE  # off by 1
    assert str(int(float(PROBE_WIDE))) != PROBE_WIDE  # off by 11
