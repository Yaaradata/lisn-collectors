"""Shortfall accounting: requested keys vs distinct source entities returned.

Three counts on collector_job (do not conflate):
  requested_count — keys we asked for (page payload length)
  returned_count  — distinct source entities that came back (not thread rows)
  record_count    — rows written to BigQuery (higher under thread explosion)
"""

from __future__ import annotations

from typing import Any

from collector.contract import Page, Record

# Cap stored key samples so large pages do not bloat jsonb.
_KEY_SAMPLE_CAP = 50


def payload_requested_keys(payload: dict[str, Any]) -> list[str]:
    """Ordered keys from an enrichment page payload (empty for discovery)."""
    for field in ("incident_ids", "order_ids", "order_item_ids"):
        values = payload.get(field)
        if isinstance(values, list):
            return [str(v) for v in values]
    return []


def requested_count(payload: dict[str, Any]) -> int:
    return len(payload_requested_keys(payload))


def _entity_id_from_record(record: Record) -> str:
    """Incident-level identity; strip thread suffix from composite keys."""
    if "::" in record.key:
        return record.key.split("::", 1)[0]
    return record.key


def returned_entity_ids(records: list[Record], payload: dict[str, Any]) -> list[str]:
    """Distinct source entities in the response, matched to the request key space.

    incident_ids  → distinct incident ids
    order_ids     → distinct orderId values on returned rows
    order_item_ids → distinct orderItemId values on returned rows
    otherwise     → distinct entity ids from Record.key
    """
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(value: Any) -> None:
        if value is None:
            return
        text = str(value)
        if text not in seen:
            seen.add(text)
            ordered.append(text)

    if isinstance(payload.get("order_ids"), list):
        for record in records:
            _add(record.data.get("orderId"))
        return ordered
    if isinstance(payload.get("order_item_ids"), list):
        for record in records:
            _add(record.data.get("orderItemId"))
        return ordered

    for record in records:
        _add(_entity_id_from_record(record))
    return ordered


def returned_count(records: list[Record], payload: dict[str, Any]) -> int:
    return len(returned_entity_ids(records, payload))


def shortfall_keys(
    page: Page, records: list[Record]
) -> dict[str, Any] | None:
    """Compare requested keys to returned entities.

    A shortfall is an ANOMALY worth surfacing, not necessarily an error — a key
    can legitimately not exist. Also records unexpected keys (returned but not
    requested) for protocol checks such as D-9.
    Stores at most the first 50 of each list plus totals.
    """
    requested = payload_requested_keys(page.payload)
    if not requested:
        return None

    returned = returned_entity_ids(records, page.payload)
    requested_set = set(requested)
    returned_set = set(returned)

    missing = [k for k in requested if k not in returned_set]
    unexpected = [k for k in returned if k not in requested_set]

    return {
        "missing": missing[:_KEY_SAMPLE_CAP],
        "missing_total": len(missing),
        "unexpected": unexpected[:_KEY_SAMPLE_CAP],
        "unexpected_total": len(unexpected),
    }
