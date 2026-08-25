"""DS-1 .. DS-15 acceptance dataset fixtures.

Design:
- Every fixture reseeds `sentinel_mock` using deterministic SQL inserts.
- `truth()` always queries sentinel_mock directly (never collector code).
- DS-11 .. DS-15 discovery windows are shaped against the fixture's seeded span.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

UTC = timezone.utc


@dataclass(frozen=True)
class SeedSnapshot:
    min_updated_on: datetime
    max_updated_on: datetime
    incident_count: int


def _dsn() -> str:
    dsn = os.environ.get("SENTINEL_MOCK_DSN", "").strip()
    if not dsn:
        raise RuntimeError("SENTINEL_MOCK_DSN is required")
    return dsn


def _connect():
    return psycopg.connect(_dsn())


def _seed_reference_universe() -> SeedSnapshot:
    """Reseed mock with a deterministic, compact universe for all DS fixtures."""
    issue_names = ("Delay in Delivery", "Damaged Product", "Wrong Item Delivered")
    statuses = (
        (100, "Unresolved", "UNRESOLVED"),
        (200, "Updated", "UNRESOLVED"),
        (300, "Solved", "RESOLVED"),
    )

    base = datetime(2026, 8, 22, 17, 13, 23, 913938, tzinfo=UTC)
    incidents: list[tuple[Any, ...]] = []
    threads: list[tuple[Any, ...]] = []
    min_updated: datetime | None = None
    max_updated: datetime | None = None

    for i in range(1, 61):
        created_at = base + timedelta(minutes=70 * i)
        updated_on = created_at + timedelta(hours=(i % 9))
        min_updated = updated_on if min_updated is None else min(min_updated, updated_on)
        max_updated = updated_on if max_updated is None else max(max_updated, updated_on)

        issue_name = issue_names[(i - 1) % len(issue_names)]
        status_id, status_status, status_type = statuses[(i - 1) % len(statuses)]
        order_id = f"OD-DS-{(i - 1) // 3:04d}"
        if i == 9:
            order_item_id = Decimal("9007199254740993")
        elif i == 10:
            order_item_id = Decimal("9007199254740995")
        else:
            order_item_id = Decimal(str(7100000000000000 + i))

        incidents.append(
            (
                f"INDS{i:05d}",
                3000 + i,
                issue_name,
                1000 + (i % 3),
                "Orders",
                order_id,
                order_item_id,
                order_item_id + Decimal("1"),
                None if i % 7 == 0 else f"FMPC{i:010d}",
                f"FSN-DS-{i:05d}",
                50 + (i % 30),
                created_at + timedelta(hours=48),
                False,
                f"SELLER-{(i % 5) + 1}",
                "sentinel",
                status_id,
                status_status,
                status_type,
                f"Dataset subject {i}",
                updated_on,
                1 + (i % 10),
                f"user_{(i % 8) + 1}",
                "IMS V2",
                f"owner_{(i % 6) + 1}",
                created_at,
            )
        )

        thread_n = 0 if i in {13, 26} else 1 + (i % 4)
        for t in range(thread_n):
            thread_created = created_at + timedelta(minutes=5 * (t + 1))
            threads.append(
                (
                    f"THR-DS-{i:05d}-{t + 1}",
                    f"INDS{i:05d}",
                    9 if t % 2 else 5,
                    "Email" if t % 2 else "Outbound",
                    Decimal(str(5000000000000000 + i * 10 + t)),
                    "text/plain",
                    thread_created,
                    "fk_crm_automation" if t % 2 else "agent_user",
                    bool(t % 2),
                    6 if t % 2 else 1,
                    "Rule Response" if t % 2 else "Note",
                    thread_created + timedelta(minutes=1),
                    "agent_user",
                )
            )

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE sentinel_thread, sentinel_incident RESTART IDENTITY CASCADE")
            cur.executemany(
                """
                INSERT INTO sentinel_incident (
                    id, issue_id, issue_name, issue_parent_id, issue_parent_name,
                    order_id, order_item_id, order_item_unit_id, tracking_id,
                    order_item_product_fsn, incident_score, resolution_deadline,
                    resolution_deadline_breach, seller_id, source,
                    status_id, status_status, status_status_type, subject,
                    updated_on, aging_score, last_updated_by_user, queue,
                    assigned_to, created_at
                ) VALUES (
                    %s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,
                    %s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s,%s,%s,
                    %s,%s
                )
                """,
                incidents,
            )
            cur.executemany(
                """
                INSERT INTO sentinel_thread (
                    thread_id, incident_id, channel_id, channel_name,
                    communication_id, content_type, created_at, created_by,
                    system_thread, thread_entry_type_id, thread_entry_type_name,
                    updated_at, updated_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                threads,
            )
        conn.commit()

    return SeedSnapshot(
        min_updated_on=min_updated or base,
        max_updated_on=max_updated or base,
        incident_count=len(incidents),
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


TruthParamsBuilder = Callable[[SeedSnapshot], dict[str, Any]]
QuerySpecBuilder = Callable[[SeedSnapshot], dict[str, Any]]


@dataclass
class DatasetFixture:
    code: str
    name: str
    description: str
    truth_sql: str
    truth_params_builder: TruthParamsBuilder
    query_spec_builder: QuerySpecBuilder
    _snapshot: SeedSnapshot | None = None

    def build(self) -> SeedSnapshot:
        self._snapshot = _seed_reference_universe()
        return self._snapshot

    def query_spec(self) -> dict[str, Any]:
        if self._snapshot is None:
            self.build()
        assert self._snapshot is not None
        return self.query_spec_builder(self._snapshot)

    def truth(self) -> list[dict[str, Any]]:
        if self._snapshot is None:
            self.build()
        assert self._snapshot is not None
        params = self.truth_params_builder(self._snapshot)
        with _connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(self.truth_sql, params)
                return [dict(r) for r in cur.fetchall()]

    def truth_sample(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.truth()
        return rows[:limit]


def _range_params(s: SeedSnapshot, *, pad_days: int = 0) -> tuple[str, str]:
    return (
        _iso(s.min_updated_on - timedelta(days=pad_days)),
        _iso(s.max_updated_on + timedelta(days=pad_days)),
    )


def _incident_ids(start: int, end: int) -> list[str]:
    return [f"INDS{i:05d}" for i in range(start, end + 1)]


def _order_ids(start_bucket: int, end_bucket: int) -> list[str]:
    return [f"OD-DS-{i:04d}" for i in range(start_bucket, end_bucket + 1)]


DATASETS: list[DatasetFixture] = [
    DatasetFixture(
        code="DS-1",
        name="incident_ids-basic",
        description="Direct incident key selection for enrichment happy path.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE id = ANY(%(ids)s)
            ORDER BY id
        """,
        truth_params_builder=lambda s: {"ids": _incident_ids(1, 10)},
        query_spec_builder=lambda s: {"incident_ids": _incident_ids(1, 10)},
    ),
    DatasetFixture(
        code="DS-2",
        name="order_ids-basic",
        description="Order key selection where one order can map to multiple incidents.",
        truth_sql="""
            SELECT id AS incident_id, order_id
            FROM sentinel_incident
            WHERE order_id = ANY(%(order_ids)s)
            ORDER BY id
        """,
        truth_params_builder=lambda s: {"order_ids": _order_ids(0, 3)},
        query_spec_builder=lambda s: {"order_ids": _order_ids(0, 3)},
    ),
    DatasetFixture(
        code="DS-3",
        name="order_item_ids-high-precision",
        description="Order-item ids including >2^53 for fidelity checks.",
        truth_sql="""
            SELECT id AS incident_id, order_item_id::text
            FROM sentinel_incident
            WHERE order_item_id = ANY(%(order_item_ids)s::numeric[])
            ORDER BY id
        """,
        truth_params_builder=lambda s: {
            "order_item_ids": [
                Decimal("9007199254740993"),
                Decimal("9007199254740995"),
                Decimal("7100000000000011"),
            ]
        },
        query_spec_builder=lambda s: {
            "order_item_ids": [9007199254740993, 9007199254740995, 7100000000000011]
        },
    ),
    DatasetFixture(
        code="DS-4",
        name="null-tracking-slice",
        description="Subset where tracking_id is NULL to validate null-bearing rows.",
        truth_sql="""
            SELECT id AS incident_id, tracking_id
            FROM sentinel_incident
            WHERE tracking_id IS NULL
            ORDER BY id
        """,
        truth_params_builder=lambda s: {},
        query_spec_builder=lambda s: {"incident_ids": _incident_ids(1, 60)},
    ),
    DatasetFixture(
        code="DS-5",
        name="missing-thread-rows",
        description="Incidents intentionally without threads (LEFT JOIN/null thread fields).",
        truth_sql="""
            SELECT i.id AS incident_id, count(t.thread_id)::int AS thread_count
            FROM sentinel_incident i
            LEFT JOIN sentinel_thread t ON t.incident_id = i.id
            GROUP BY i.id
            HAVING count(t.thread_id) = 0
            ORDER BY i.id
        """,
        truth_params_builder=lambda s: {},
        query_spec_builder=lambda s: {"incident_ids": ["INDS00013", "INDS00026"]},
    ),
    DatasetFixture(
        code="DS-6",
        name="status-unresolved-window",
        description="Discovery-like subset by unresolved statuses.",
        truth_sql="""
            SELECT id AS incident_id, status_status
            FROM sentinel_incident
            WHERE status_status = 'Unresolved'
            ORDER BY id
        """,
        truth_params_builder=lambda s: {},
        query_spec_builder=lambda s: {"statuses": ["Unresolved"]},
    ),
    DatasetFixture(
        code="DS-7",
        name="issue-delay-window",
        description="Discovery-like subset by issue name.",
        truth_sql="""
            SELECT id AS incident_id, issue_name
            FROM sentinel_incident
            WHERE issue_name = 'Delay in Delivery'
            ORDER BY id
        """,
        truth_params_builder=lambda s: {},
        query_spec_builder=lambda s: {"issue_names": ["Delay in Delivery"]},
    ),
    DatasetFixture(
        code="DS-8",
        name="status-plus-issue-intersection",
        description="Intersection filter (status + issue) for discovery truth.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE status_status = 'Updated'
              AND issue_name = 'Damaged Product'
            ORDER BY id
        """,
        truth_params_builder=lambda s: {},
        query_spec_builder=lambda s: {
            "statuses": ["Updated"],
            "issue_names": ["Damaged Product"],
        },
    ),
    DatasetFixture(
        code="DS-9",
        name="discovery-cursor-page-1",
        description="First page in id-keyset discovery order.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE updated_on >= %(start)s::timestamptz
              AND updated_on <= %(end)s::timestamptz
            ORDER BY id
            LIMIT 25
        """,
        truth_params_builder=lambda s: dict(zip(("start", "end"), _range_params(s))),
        query_spec_builder=lambda s: {
            "updated_from": _range_params(s)[0],
            "updated_to": _range_params(s)[1],
            "limit": 25,
        },
    ),
    DatasetFixture(
        code="DS-10",
        name="discovery-cursor-page-2",
        description="Second page in id-keyset discovery order.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE updated_on >= %(start)s::timestamptz
              AND updated_on <= %(end)s::timestamptz
              AND id > %(cursor)s
            ORDER BY id
            LIMIT 25
        """,
        truth_params_builder=lambda s: {
            "start": _range_params(s)[0],
            "end": _range_params(s)[1],
            "cursor": "INDS00025",
        },
        query_spec_builder=lambda s: {
            "updated_from": _range_params(s)[0],
            "updated_to": _range_params(s)[1],
            "limit": 25,
            "cursor": "INDS00025",
        },
    ),
    DatasetFixture(
        code="DS-11",
        name="discovery-full-span-plus-one-day",
        description="Discovery window spanning full seeded updated_on range +1 day each side.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE updated_on >= %(start)s::timestamptz
              AND updated_on <= %(end)s::timestamptz
            ORDER BY id
        """,
        truth_params_builder=lambda s: dict(zip(("start", "end"), _range_params(s, pad_days=1))),
        query_spec_builder=lambda s: {
            "updated_from": _range_params(s, pad_days=1)[0],
            "updated_to": _range_params(s, pad_days=1)[1],
            "limit": 1000,
        },
    ),
    DatasetFixture(
        code="DS-12",
        name="discovery-pre-span-empty",
        description="Discovery window entirely before seeded updated_on range (expected empty).",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE updated_on >= %(start)s::timestamptz
              AND updated_on <= %(end)s::timestamptz
            ORDER BY id
        """,
        truth_params_builder=lambda s: {
            "start": _iso(s.min_updated_on - timedelta(days=4)),
            "end": _iso(s.min_updated_on - timedelta(days=2)),
        },
        query_spec_builder=lambda s: {
            "updated_from": _iso(s.min_updated_on - timedelta(days=4)),
            "updated_to": _iso(s.min_updated_on - timedelta(days=2)),
            "limit": 1000,
        },
    ),
    DatasetFixture(
        code="DS-13",
        name="discovery-mid-span-window",
        description="Discovery window focused inside seeded span.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE updated_on >= %(start)s::timestamptz
              AND updated_on <= %(end)s::timestamptz
            ORDER BY id
        """,
        truth_params_builder=lambda s: {
            "start": _iso(s.min_updated_on + timedelta(hours=18)),
            "end": _iso(s.max_updated_on - timedelta(hours=18)),
        },
        query_spec_builder=lambda s: {
            "updated_from": _iso(s.min_updated_on + timedelta(hours=18)),
            "updated_to": _iso(s.max_updated_on - timedelta(hours=18)),
            "limit": 1000,
        },
    ),
    DatasetFixture(
        code="DS-14",
        name="discovery-post-span-empty",
        description="Discovery window entirely after seeded updated_on range (expected empty).",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE updated_on >= %(start)s::timestamptz
              AND updated_on <= %(end)s::timestamptz
            ORDER BY id
        """,
        truth_params_builder=lambda s: {
            "start": _iso(s.max_updated_on + timedelta(days=2)),
            "end": _iso(s.max_updated_on + timedelta(days=4)),
        },
        query_spec_builder=lambda s: {
            "updated_from": _iso(s.max_updated_on + timedelta(days=2)),
            "updated_to": _iso(s.max_updated_on + timedelta(days=4)),
            "limit": 1000,
        },
    ),
    DatasetFixture(
        code="DS-15",
        name="discovery-created-window-with-filters",
        description="Discovery using created_at window plus status/issue filters.",
        truth_sql="""
            SELECT id AS incident_id
            FROM sentinel_incident
            WHERE created_at >= %(created_from)s::timestamptz
              AND created_at <= %(created_to)s::timestamptz
              AND status_status = ANY(%(statuses)s)
              AND issue_name = ANY(%(issues)s)
            ORDER BY id
        """,
        truth_params_builder=lambda s: {
            "created_from": _iso(s.min_updated_on - timedelta(hours=12)),
            "created_to": _iso(s.max_updated_on - timedelta(hours=6)),
            "statuses": ["Unresolved", "Updated"],
            "issues": ["Delay in Delivery", "Wrong Item Delivered"],
        },
        query_spec_builder=lambda s: {
            "created_from": _iso(s.min_updated_on - timedelta(hours=12)),
            "created_to": _iso(s.max_updated_on - timedelta(hours=6)),
            "statuses": ["Unresolved", "Updated"],
            "issue_names": ["Delay in Delivery", "Wrong Item Delivered"],
            "limit": 1000,
        },
    ),
]

