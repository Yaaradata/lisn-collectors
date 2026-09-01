"""Source collector contract — the extension point for every Flipkart source.

Adding collector #2 (eKart, FDP, …) should cost one new source module that
implements SourceCollector, not a rewrite of the request API or workers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Page:
    page_no: int
    payload: dict


@dataclass(frozen=True)
class RawResponse:
    body: bytes
    content_type: str
    # Optional HTTP metadata for source_fetch span attributes (host only, never
    # a full URL that could embed keys).
    http_status_code: int | None = None
    url_host: str | None = None


@dataclass(frozen=True)
class Record:
    key: str
    data: dict


class MalformedSourcePayload(ValueError):
    """Source returned bytes that are not the expected JSON shape.

    Distinct from transport failures (HTTP errors, timeouts) so retries and
    operators can tell "the source answered wrongly" from "the source was
    unreachable". Raised from parse() after the raw write, never from fetch().
    """


@runtime_checkable
class SourceCollector(Protocol):
    """Per-source collector: identity, limits, and plan / fetch / parse."""

    name: str
    """Source identifier; also the Procrastinate queue name."""

    batch_cap: int
    """Max keys per call to the source (e.g. Multi Track / Sentinel 50)."""

    min_interval_s: float
    """Sleep between calls; the rate throttle."""

    lease_seconds: int
    """Worker lease length; longer than the slowest page for this source."""

    max_attempts: int
    """Attempts before a job is dead-lettered."""

    bq_table: str
    """Fully qualified BigQuery landing table (e.g. project.dataset.table)."""

    def plan(self, query_spec: dict) -> list[Page]:
        """Split one LiSN query into pages.

        WHEN: runs in the API process at request time, never at fetch time.
        WHY separate: recovery must never re-derive a different page list; the
        pages are fixed when the request is accepted and stored on collector_job.
        """
        ...

    def fetch(self, page: Page) -> RawResponse:
        """One call to the source. Must be idempotent.

        WHEN: runs in a worker for a single collector_job page.
        WHY separate: this is the only method that changes if the real Sentinel
        turns out to be a CSV download rather than an API — that is the whole
        argument for the contract. State, leasing, raw landing, and parse stay put.
        """
        ...

    def parse(self, raw: RawResponse, page: Page) -> list[Record]:
        """Bytes to records. No I/O, no side effects.

        WHEN: runs after a successful fetch, against the raw bytes (or when
        reprocessing stored raw).
        WHY separate: pure so it can be unit tested with a fixture and re-run
        over stored raw bytes when a mapping is corrected.
        """
        ...
