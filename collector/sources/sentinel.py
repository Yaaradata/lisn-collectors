"""Sentinel source collector — the only per-source implementation for the demo."""

from __future__ import annotations

import json
import os
from typing import Any

from collector.contract import MalformedSourcePayload, Page, RawResponse, Record
from collector.http import get_client

# Identifiers at incident grain / join keys. Must stay strings end-to-end —
# float/int coercion above 2^53 silently mutates the key.
_ID_STRING_FIELDS = (
    "orderItemId",
    "orderItemUnitId",
    "threads_communicationId",
)


def _flatten_dotted(row: dict) -> dict:
    """Turn dotted export keys into BigQuery-safe underscore names."""
    flat: dict = {}
    for key, value in row.items():
        flat[key.replace(".", "_")] = value
    return flat


def _pass_through_id_string(field: str, value: Any) -> str | None:
    """Keep identifiers as strings. NULL → None. Never float() / int()."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise MalformedSourcePayload(
            f"sentinel {field} must not be bool, got {value!r}"
        )
    if isinstance(value, float):
        # Already rounded on the wire if it arrived as a JSON number.
        raise MalformedSourcePayload(
            f"sentinel {field} arrived as float — identifiers must be JSON "
            "strings (float loses precision above 2^53)"
        )
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        # Accept int only so a still-exact JSON number below 2^53 can land;
        # do not go through float. Prefer string on the wire.
        return str(value)
    raise MalformedSourcePayload(
        f"sentinel {field} must be string or null, got {type(value).__name__}"
    )


class SentinelCollector:
    name = "sentinel"
    # Multi Track states this on screen; assumed for Sentinel until Flipkart confirms.
    batch_cap = 50
    # Rate ceiling is instances x (1/interval).
    min_interval_s = 1.0
    lease_seconds = 300
    max_attempts = 3
    bq_table = "sentinel_raw.incidents_v2"

    def plan(self, query_spec: dict) -> list[Page]:
        # This rejection is a hard product rule, not defensive coding. Queries are
        # per-key, by incident ID, order-item ID, or order ID. Flipkart's domain
        # advisor: an incident is created against an ORDER ITEM ("the currency
        # would be on order item id"); a multi-item order yields one incident per
        # item. The console also exposes incident / order multi-value filters.
        for name, values in (
            ("incident_ids", query_spec.get("incident_ids")),
            ("order_item_ids", query_spec.get("order_item_ids")),
            ("order_ids", query_spec.get("order_ids")),
        ):
            if values is not None and not isinstance(values, list):
                raise ValueError(
                    f"{name} must be a list, got {type(values).__name__}"
                )

        incident_ids = list(query_spec.get("incident_ids") or [])
        order_item_ids = list(query_spec.get("order_item_ids") or [])
        order_ids = list(query_spec.get("order_ids") or [])

        supplied: list[str] = []
        if incident_ids:
            supplied.append("incident_ids")
        if order_item_ids:
            supplied.append("order_item_ids")
        if order_ids:
            supplied.append("order_ids")

        if len(supplied) > 1:
            raise ValueError(
                "exactly one of incident_ids, order_item_ids, order_ids required; "
                f"got {', '.join(supplied)}"
            )
        if not supplied:
            raise ValueError(
                "incident_ids, order_item_ids or order_ids required — no generic queries"
            )

        field = supplied[0]
        keys = {
            "incident_ids": incident_ids,
            "order_item_ids": order_item_ids,
            "order_ids": order_ids,
        }[field]

        pages: list[Page] = []
        for page_no, start in enumerate(range(0, len(keys), self.batch_cap)):
            chunk = keys[start : start + self.batch_cap]
            pages.append(Page(page_no=page_no, payload={field: chunk}))
        return pages

    def fetch(self, page: Page) -> RawResponse:
        # Return the bytes EXACTLY as received. They are written to GCS unmodified
        # — that raw object is our evidence of what the source returned.
        # get_client is shared rather than per-source: every source that talks to
        # a Cloud Run service needs the same ID-token behaviour.
        base = os.environ["SENTINEL_URL"].rstrip("/")
        url = f"{base}/v1/incidents/search"
        with get_client(base) as client:
            response = client.post(url, json=page.payload, timeout=30.0)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "application/json")
        return RawResponse(body=response.content, content_type=content_type)

    def parse(self, raw: RawResponse, page: Page) -> list[Record]:
        # Shape checks belong HERE, not in fetch(). fetch() returns raw bytes
        # untouched so the GCS object always lands as evidence even when the
        # payload is garbage. Validation runs after the raw write (tasks.py
        # step 3 then step 4), so we keep proof of what the source actually sent
        # while still failing the page — retry, then dead-letter — instead of
        # iterating a string into character "records".
        #
        # The composite key exists because the Sentinel export is THREAD-EXPLODED.
        # One incident returns one row per conversation thread — our seed data
        # measured a factor of 2.481. Keying on incident id alone would silently
        # collapse conversation history.
        del page  # unused; signature required by SourceCollector
        doc = json.loads(raw.body.decode("utf-8"))
        if not isinstance(doc, dict):
            raise MalformedSourcePayload(
                f"sentinel returned {type(doc).__name__}, expected dict"
            )
        if "incidents" not in doc:
            raise MalformedSourcePayload("sentinel response missing 'incidents'")
        incidents = doc["incidents"]
        if not isinstance(incidents, list):
            raise MalformedSourcePayload(
                f"sentinel returned incidents as {type(incidents).__name__}, "
                "expected list"
            )
        records: list[Record] = []
        for index, row in enumerate(incidents):
            if not isinstance(row, dict):
                raise MalformedSourcePayload(
                    f"sentinel incidents[{index}] is {type(row).__name__}, "
                    "expected dict"
                )
            if "id" not in row:
                raise MalformedSourcePayload(
                    f"sentinel incidents[{index}] missing 'id'"
                )
            flat = _flatten_dotted(row)
            for field in _ID_STRING_FIELDS:
                if field in flat:
                    flat[field] = _pass_through_id_string(field, flat[field])
            incident_id = row["id"]
            thread_id = row.get("threads.id") or "none"
            key = f"{incident_id}::{thread_id}"
            records.append(Record(key=key, data=flat))
        return records
