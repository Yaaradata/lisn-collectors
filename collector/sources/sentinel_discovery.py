"""Sentinel discovery collector — filter → ids (enrichment is sentinel.py).

THE POINT: prove SourceCollector absorbs a fundamentally different query shape.
Discovery takes a filter and returns ids; enrichment takes ids and returns data.
plan/fetch/parse express both without changing contract.py.

Continuation choice: (b) — fetch() loops cursors internally.
  Why: the task body cannot enqueue follow-up pages from parse(), and plan()
  cannot know the match count up front. Following cursors inside fetch() keeps
  one collector_job = one unit of work, applies min_interval_s between calls
  (honest rate ceiling), and needs no LiSN/orchestration outside the collector.
  Alternative (a) would emit a next_cursor sentinel for an external caller to
  re-submit — cheaper per job, but splits one discovery into N API requests and
  pushes paging policy out of the collector.
  Trade-off: a wide filter hits CURSOR_PAGE_CAP and returns PARTIAL results; the
  cap is visible on every Record (partial=true) rather than silent truncation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from collector.contract import MalformedSourcePayload, Page, RawResponse, Record
from collector.http import get_client
from collector.metrics import record_source_call, record_source_latency
from collector.tracing import traced_span, url_host

MAX_WINDOW = timedelta(days=15)
# Hard cap on cursor-followed discovery pages inside one fetch() job.
CURSOR_PAGE_CAP = 10

_FILTER_FIELDS = (
    "updated_from",
    "updated_to",
    "created_from",
    "created_to",
    "statuses",
    "issue_names",
    "limit",
)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_dt(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, str):
        raw = value.replace("Z", "+00:00")
        try:
            return _ensure_aware(datetime.fromisoformat(raw))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 datetime, got {value!r}") from exc
    raise ValueError(f"{field} must be an ISO-8601 datetime, got {type(value).__name__}")


def _validate_window(
    name: str, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime] | None:
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise ValueError(
            f"{name}_from and {name}_to must both be set for a {name} window"
        )
    if end < start:
        raise ValueError(f"{name} window end must be on or after start")
    if end - start > MAX_WINDOW:
        raise ValueError(
            f"{name} window exceeds the 15-day limit "
            f"(got {(end - start).days} days)"
        )
    return start, end


def _filter_hash(filter_spec: dict[str, Any]) -> str:
    """Stable hash of the filter so later queries can ask 'this exact filter'."""
    canonical = {k: filter_spec[k] for k in _FILTER_FIELDS if k in filter_spec}
    blob = json.dumps(canonical, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SentinelDiscoveryCollector:
    name = "sentinel_discovery"
    batch_cap = 1000  # ids per discovery page, not keys per enrichment query
    min_interval_s = 2.0  # slower than enrichment; discovery is a heavier query
    lease_seconds = 600  # a wide window can take longer than an id lookup
    max_attempts = 3
    bq_table = "sentinel_raw.discovered_ids"

    def plan(self, query_spec: dict) -> list[Page]:
        updated = _validate_window(
            "updated",
            _parse_dt(query_spec.get("updated_from"), "updated_from"),
            _parse_dt(query_spec.get("updated_to"), "updated_to"),
        )
        created = _validate_window(
            "created",
            _parse_dt(query_spec.get("created_from"), "created_from"),
            _parse_dt(query_spec.get("created_to"), "created_to"),
        )
        if updated is None and created is None:
            raise ValueError(
                "at least one time window required "
                "(updated_from/updated_to or created_from/created_to)"
            )

        limit = query_spec.get("limit", self.batch_cap)
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError(f"limit must be an int, got {limit!r}")
        if limit < 1 or limit > 5000:
            raise ValueError(f"limit must be between 1 and 5000, got {limit}")

        payload: dict[str, Any] = {"limit": limit}
        for key in (
            "updated_from",
            "updated_to",
            "created_from",
            "created_to",
            "statuses",
            "issue_names",
        ):
            if key in query_spec and query_spec[key] is not None:
                payload[key] = query_spec[key]

        # Discovery is inherently sequential: page N+1's cursor comes from page
        # N's response, so unlike enrichment these cannot be fanned out in
        # parallel. plan() therefore emits ONE page; fetch() follows cursors
        # inside that single job (see module docstring, choice (b)).
        # Page count is unknown up front — enrichment knows 1000 ids / 50 = 20
        # pages; discovery does not know how many incidents match until it asks.
        return [Page(page_no=0, payload=payload)]

    def fetch(self, page: Page) -> RawResponse:
        # Return bytes that land in GCS unmodified (our evidence of what the
        # source returned). Envelope wraps one-or-more discover responses so
        # parse() can see partial / cursor_pages_fetched after the internal loop.
        #
        # One child span PER CURSOR PAGE — not per incident id. Row-level spans
        # at discovery volume would drown SigNoz; the sequential walk cost is
        # what we need visible against parallel enrichment fan-out.
        base = os.environ["SENTINEL_URL"].rstrip("/")
        url = f"{base}/v1/incidents/discover"
        host = url_host(url)

        filter_body = dict(page.payload)
        pages_out: list[dict[str, Any]] = []
        cursor: str | None = None
        partial = False
        last_status = 0

        with traced_span(
            "source_fetch",
            attributes={"source.url_host": host},
        ) as fetch_span:
            with get_client(base) as client:
                for i in range(CURSOR_PAGE_CAP):
                    if i > 0:
                        time.sleep(self.min_interval_s)
                    body = dict(filter_body)
                    if cursor is not None:
                        body["cursor"] = cursor
                    with traced_span(
                        "discovery_cursor_page",
                        attributes={
                            "discovery.cursor_page_index": i,
                            "source.url_host": host,
                        },
                    ) as cursor_span:
                        t0 = time.perf_counter()
                        try:
                            response = client.post(url, json=body, timeout=60.0)
                            record_source_call(
                                source=self.name,
                                http_status=response.status_code,
                            )
                            record_source_latency(
                                source=self.name,
                                duration_ms=(time.perf_counter() - t0) * 1000.0,
                            )
                        except Exception:
                            record_source_call(
                                source=self.name, http_status=0
                            )
                            record_source_latency(
                                source=self.name,
                                duration_ms=(time.perf_counter() - t0) * 1000.0,
                            )
                            raise
                        last_status = response.status_code
                        cursor_span.set_attribute(
                            "http.status_code", response.status_code
                        )
                        cursor_span.set_attribute(
                            "http.response_content_length", len(response.content)
                        )
                        response.raise_for_status()
                        doc = response.json()
                        pages_out.append(doc)
                        ids = doc.get("incident_ids") if isinstance(doc, dict) else None
                        if isinstance(ids, list):
                            cursor_span.set_attribute(
                                "discovery.ids_in_page", len(ids)
                            )
                    if not doc.get("has_more"):
                        break
                    cursor = doc.get("next_cursor")
                    if not cursor:
                        break
                else:
                    # Exhausted CURSOR_PAGE_CAP while has_more was still true.
                    partial = True

            fetch_span.set_attribute("http.status_code", last_status)
            fetch_span.set_attribute(
                "discovery.cursor_pages_fetched", len(pages_out)
            )
            fetch_span.set_attribute("discovery.partial", partial)

        envelope = {
            "pages": pages_out,
            "partial": partial,
            "cursor_pages_fetched": len(pages_out),
            "cursor_page_cap": CURSOR_PAGE_CAP,
            "filter": filter_body,
        }
        raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        return RawResponse(
            body=raw,
            content_type="application/json",
            http_status_code=last_status or None,
            url_host=host,
        )

    def parse(self, raw: RawResponse, page: Page) -> list[Record]:
        # Shape checks belong HERE, not in fetch(). fetch() returns (or builds)
        # raw bytes that land in GCS unmodified — evidence of what the source
        # sent — even when the payload is garbage. Validation runs after that
        # write so a string mistaken for a list cannot silently become
        # character-level "records".
        doc = json.loads(raw.body.decode("utf-8"))
        if not isinstance(doc, dict):
            raise MalformedSourcePayload(
                f"sentinel_discovery returned {type(doc).__name__}, expected dict"
            )
        if "pages" not in doc:
            raise MalformedSourcePayload(
                "sentinel_discovery response missing 'pages'"
            )
        pages = doc["pages"]
        if not isinstance(pages, list):
            raise MalformedSourcePayload(
                f"sentinel_discovery returned pages as {type(pages).__name__}, "
                "expected list"
            )

        filter_spec = doc.get("filter") or page.payload
        fhash = _filter_hash(filter_spec)
        discovered_at = datetime.now(timezone.utc).isoformat()
        partial = bool(doc.get("partial"))
        cursor_pages_fetched = int(doc.get("cursor_pages_fetched") or 0)
        cursor_page_cap = int(doc.get("cursor_page_cap") or CURSOR_PAGE_CAP)

        records: list[Record] = []
        for page_index, chunk in enumerate(pages):
            if not isinstance(chunk, dict):
                raise MalformedSourcePayload(
                    f"sentinel_discovery pages[{page_index}] is "
                    f"{type(chunk).__name__}, expected dict"
                )
            if "incident_ids" not in chunk:
                raise MalformedSourcePayload(
                    f"sentinel_discovery pages[{page_index}] missing 'incident_ids'"
                )
            incident_ids = chunk["incident_ids"]
            if not isinstance(incident_ids, list):
                raise MalformedSourcePayload(
                    f"sentinel_discovery returned incident_ids as "
                    f"{type(incident_ids).__name__}, expected list"
                )
            for index, incident_id in enumerate(incident_ids):
                if not isinstance(incident_id, str):
                    raise MalformedSourcePayload(
                        f"sentinel_discovery pages[{page_index}]."
                        f"incident_ids[{index}] is {type(incident_id).__name__}, "
                        "expected str"
                    )
                records.append(
                    Record(
                        key=incident_id,
                        data={
                            "incident_id": incident_id,
                            "discovered_at": discovered_at,
                            "filter_hash": fhash,
                            "cursor_page": page.page_no,
                            "partial": partial,
                            "cursor_pages_fetched": cursor_pages_fetched,
                            "cursor_page_cap": cursor_page_cap,
                        },
                    )
                )
        return records
