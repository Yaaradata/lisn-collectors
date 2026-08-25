"""Sentinel source collector — the only per-source implementation for the demo."""

from __future__ import annotations

import json
import os

from collector.contract import Page, RawResponse, Record
from collector.http import get_client


def _flatten_dotted(row: dict) -> dict:
    """Turn dotted export keys into BigQuery-safe underscore names."""
    flat: dict = {}
    for key, value in row.items():
        flat[key.replace(".", "_")] = value
    return flat


class SentinelCollector:
    name = "sentinel"
    # Multi Track states this on screen; assumed for Sentinel until Flipkart confirms.
    batch_cap = 50
    # Rate ceiling is instances x (1/interval).
    min_interval_s = 1.0
    lease_seconds = 300
    max_attempts = 3
    bq_table = "sentinel_raw.incidents"

    def plan(self, query_spec: dict) -> list[Page]:
        # This rejection is a hard product rule, not defensive coding. Queries are
        # per-key, by incident ID, order-item ID, or order ID. Flipkart's domain
        # advisor: an incident is created against an ORDER ITEM ("the currency
        # would be on order item id"); a multi-item order yields one incident per
        # item. The console also exposes incident / order multi-value filters.
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
        # The composite key exists because the Sentinel export is THREAD-EXPLODED.
        # One incident returns one row per conversation thread — our seed data
        # measured a factor of 2.481. Keying on incident id alone would silently
        # collapse conversation history.
        del page  # unused; signature required by SourceCollector
        doc = json.loads(raw.body.decode("utf-8"))
        records: list[Record] = []
        for row in doc.get("incidents") or []:
            flat = _flatten_dotted(row)
            incident_id = row["id"]
            thread_id = row.get("threads.id") or "none"
            key = f"{incident_id}::{thread_id}"
            records.append(Record(key=key, data=flat))
        return records
