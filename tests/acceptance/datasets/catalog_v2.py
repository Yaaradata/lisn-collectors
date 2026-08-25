"""v2 acceptance datasets DS-1 .. DS-15.

All truth data comes directly from sentinel_mock SQL (never collector paths).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[3]


def _dsn() -> str:
    dsn = os.environ.get("SENTINEL_MOCK_DSN", "").strip()
    if not dsn:
        raise RuntimeError("SENTINEL_MOCK_DSN is required")
    return dsn


def _connect():
    return psycopg.connect(_dsn())


@dataclass(frozen=True)
class SeedSnapshot:
    min_updated_on: datetime
    max_updated_on: datetime
    incident_count: int
    thread_count: int


def _snapshot() -> SeedSnapshot:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(updated_on), max(updated_on), count(*) FROM sentinel_incident")
            min_u, max_u, n_inc = cur.fetchone()
            cur.execute("SELECT count(*) FROM sentinel_thread")
            n_thr = int(cur.fetchone()[0])
    return SeedSnapshot(
        min_updated_on=min_u,
        max_updated_on=max_u,
        incident_count=int(n_inc),
        thread_count=n_thr,
    )


def _run_seed(
    *,
    n_incidents: int | None = None,
    start_date: str = "2026-08-22",
    end_date: str = "2026-08-25",
    min_per_day: int | None = None,
    max_per_day: int | None = None,
) -> SeedSnapshot:
    env = os.environ.copy()
    env["SEED_START_DATE"] = start_date
    env["SEED_END_DATE"] = end_date
    if n_incidents is None:
        # Keep empty so mock.seed_sentinel's dotenv loader does not re-inject
        # N_INCIDENTS=1000 from .env.
        env["N_INCIDENTS"] = ""
    else:
        env["N_INCIDENTS"] = str(n_incidents)
    if min_per_day is not None:
        env["SEED_MIN_PER_DAY"] = str(min_per_day)
    if max_per_day is not None:
        env["SEED_MAX_PER_DAY"] = str(max_per_day)
    cmd = [str(REPO_ROOT / ".venv/bin/python"), "-m", "mock.seed_sentinel"]
    subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=True, capture_output=True, text=True)
    return _snapshot()


def _incident_ids(limit: int) -> list[str]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT %s", (limit,))
            return [r[0] for r in cur.fetchall()]


def _first_ids(limit: int) -> list[str]:
    return _incident_ids(limit)


def _set_even_updated_span(hours: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id")
            ids = [r[0] for r in cur.fetchall()]
            base = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
            if len(ids) < 2:
                step = 0
            else:
                step = (hours * 3600) / (len(ids) - 1)
            payload = []
            for i, incident_id in enumerate(ids):
                payload.append((base + timedelta(seconds=step * i), incident_id))
            cur.executemany("UPDATE sentinel_incident SET updated_on=%s WHERE id=%s", payload)
        conn.commit()


def _apply_ds3_skew() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 2")
            heavy_id, zero_id = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM sentinel_thread WHERE incident_id IN (%s,%s)", (heavy_id, zero_id))
            heavy_rows = []
            t0 = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
            for i in range(500):
                heavy_rows.append(
                    (
                        f"THR-{heavy_id}-{i+1:04d}",
                        heavy_id,
                        9,
                        "Email",
                        Decimal(10_000_000_000_000_000 + i),
                        "text/plain",
                        t0 + timedelta(seconds=i),
                        "load-test",
                        False,
                        1,
                        "Note",
                        t0 + timedelta(seconds=i + 1),
                        "load-test",
                    )
                )
            cur.executemany(
                """
                INSERT INTO sentinel_thread (
                    thread_id, incident_id, channel_id, channel_name, communication_id,
                    content_type, created_at, created_by, system_thread,
                    thread_entry_type_id, thread_entry_type_name, updated_at, updated_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                heavy_rows,
            )
        conn.commit()


def _apply_ds4_sparse() -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id")
            ids = [r[0] for r in cur.fetchall()]
            n = len(ids)
            null_tracking = set(ids[: int(0.30 * n)])
            no_threads = set(ids[int(0.30 * n) : int(0.45 * n)])
            null_order_item = set(ids[int(0.45 * n) : int(0.55 * n)])
            cur.execute("UPDATE sentinel_incident SET tracking_id=NULL WHERE id = ANY(%s)", (list(null_tracking),))
            cur.execute("DELETE FROM sentinel_thread WHERE incident_id = ANY(%s)", (list(no_threads),))
            cur.execute("UPDATE sentinel_incident SET order_item_id=NULL WHERE id = ANY(%s)", (list(null_order_item),))
        conn.commit()


def _apply_ds5_dirty_text() -> None:
    dirty = [
        "अत्यंत विलंबित डिलिवरी 🚚",
        "Subject with quote \" and brace } and json {\"incidents\":[]}",
        "Emoji burst 😅🔥✅ and control \\u0001 char",
        "X" * 4000,
    ]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 20")
            ids = [r[0] for r in cur.fetchall()]
            updates = [(dirty[i % len(dirty)], ids[i]) for i in range(len(ids))]
            cur.executemany("UPDATE sentinel_incident SET subject=%s WHERE id=%s", updates)
        conn.commit()


def _apply_ds6_numeric_boundaries() -> None:
    targets = [
        Decimal("9007199254740991"),
        Decimal("9007199254740993"),
        Decimal("1234567890123456789"),
        Decimal("-1234567890"),
        Decimal("123"),
    ]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 5")
            ids = [r[0] for r in cur.fetchall()]
            cur.executemany(
                "UPDATE sentinel_incident SET order_item_id=%s WHERE id=%s",
                list(zip(targets, ids)),
            )
        conn.commit()


BuildFn = Callable[[], SeedSnapshot]
TruthParamsFn = Callable[[SeedSnapshot], dict[str, Any]]


@dataclass
class DatasetFixture:
    code: str
    shape: str
    risk: str
    build_fn: BuildFn
    truth_sql: str
    truth_params_fn: TruthParamsFn
    _snapshot: SeedSnapshot | None = None

    def build(self) -> SeedSnapshot:
        self._snapshot = self.build_fn()
        return self._snapshot

    def truth(self) -> list[dict[str, Any]]:
        if self._snapshot is None:
            self.build()
        assert self._snapshot is not None
        with _connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(self.truth_sql, self.truth_params_fn(self._snapshot))
                return [dict(r) for r in cur.fetchall()]

    def truth_count(self) -> int:
        if self._snapshot is None:
            self.build()
        assert self._snapshot is not None
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT count(*)::bigint FROM ({self.truth_sql}) AS truth_rows",
                    self.truth_params_fn(self._snapshot),
                )
                return int(cur.fetchone()[0])

def _build_ds1() -> SeedSnapshot:
    return _run_seed(n_incidents=1000)


def _build_ds2() -> SeedSnapshot:
    return _run_seed(n_incidents=None, start_date="2026-08-18", end_date="2026-08-25")


def _build_ds3() -> SeedSnapshot:
    _run_seed(n_incidents=1000)
    _apply_ds3_skew()
    return _snapshot()


def _build_ds4() -> SeedSnapshot:
    _run_seed(n_incidents=1000)
    _apply_ds4_sparse()
    return _snapshot()


def _build_ds5() -> SeedSnapshot:
    _run_seed(n_incidents=1000)
    _apply_ds5_dirty_text()
    return _snapshot()


def _build_ds6() -> SeedSnapshot:
    _run_seed(n_incidents=1000)
    _apply_ds6_numeric_boundaries()
    return _snapshot()


def _build_ds7() -> SeedSnapshot:
    _run_seed(n_incidents=1000)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 4")
            ids = [r[0] for r in cur.fetchall()]
            cur.execute(
                "UPDATE sentinel_incident SET order_id='OD-COLLIDE-1' WHERE id = ANY(%s)",
                (ids,),
            )
            t_ref = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
            cur.execute("UPDATE sentinel_incident SET updated_on=%s WHERE id IN (%s,%s)", (t_ref, ids[0], ids[1]))
        conn.commit()
    return _snapshot()


def _build_ds8() -> SeedSnapshot:
    return _run_seed(n_incidents=1000)


def _build_ds9() -> SeedSnapshot:
    return _run_seed(n_incidents=1000)


def _build_ds10() -> SeedSnapshot:
    return _run_seed(n_incidents=1000)


def _build_ds11() -> SeedSnapshot:
    _run_seed(n_incidents=360, start_date="2026-08-25", end_date="2026-08-25")
    _set_even_updated_span(6)
    return _snapshot()


def _build_ds12() -> SeedSnapshot:
    _build_ds11()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id LIMIT 4")
            ids = [r[0] for r in cur.fetchall()]
            w_start = datetime(2026, 8, 25, 1, 0, 0, tzinfo=UTC)
            w_end = datetime(2026, 8, 25, 2, 0, 0, tzinfo=UTC)
            cur.execute("UPDATE sentinel_incident SET updated_on=%s WHERE id=%s", (w_start, ids[0]))
            cur.execute("UPDATE sentinel_incident SET updated_on=%s WHERE id=%s", (w_end, ids[1]))
            cur.execute(
                "UPDATE sentinel_incident SET updated_on=%s WHERE id=%s",
                (w_start - timedelta(microseconds=1), ids[2]),
            )
            cur.execute(
                "UPDATE sentinel_incident SET updated_on=%s WHERE id=%s",
                (w_end + timedelta(microseconds=1), ids[3]),
            )
            cur.execute("UPDATE sentinel_incident SET subject='DS12-WINDOW-START' WHERE id=%s", (ids[0],))
            cur.execute("UPDATE sentinel_incident SET subject='DS12-WINDOW-END' WHERE id=%s", (ids[1],))
            cur.execute("UPDATE sentinel_incident SET subject='DS12-WINDOW-BEFORE' WHERE id=%s", (ids[2],))
            cur.execute("UPDATE sentinel_incident SET subject='DS12-WINDOW-AFTER' WHERE id=%s", (ids[3],))
        conn.commit()
    return _snapshot()


def _build_ds13() -> SeedSnapshot:
    _run_seed(n_incidents=5000, start_date="2026-08-25", end_date="2026-08-25")
    _set_even_updated_span(1)
    return _snapshot()


def _build_ds14() -> SeedSnapshot:
    return _build_ds11()


def _build_ds15() -> SeedSnapshot:
    _run_seed(n_incidents=120, start_date="2026-08-25", end_date="2026-08-25")
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM sentinel_incident ORDER BY id")
            ids = [r[0] for r in cur.fetchall()]
            bulk = []
            base = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
            for i, incident_id in enumerate(ids):
                if i == 0:
                    upd = base + timedelta(hours=5, minutes=59)
                else:
                    upd = base + timedelta(minutes=i % 30)
                bulk.append((upd, incident_id))
            cur.executemany("UPDATE sentinel_incident SET updated_on=%s WHERE id=%s", bulk)
        conn.commit()
    return _snapshot()


def _noop(_: SeedSnapshot) -> dict[str, Any]:
    return {}


def _truth_ids(_: SeedSnapshot, limit: int) -> dict[str, Any]:
    return {"ids": _first_ids(limit)}


DATASETS: list[DatasetFixture] = [
    DatasetFixture("DS-1", "Baseline 1,000 incidents default thread distribution", "Normal operation reference", _build_ds1, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id ORDER BY i.id, t.thread_id NULLS LAST", _noop),
    DatasetFixture("DS-2", "Population full date-range seed", "Cycle-window fit at population scale", _build_ds2, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id ORDER BY i.id, t.thread_id NULLS LAST", _noop),
    DatasetFixture("DS-3", "Thread skew: one 500-thread, one 0-thread", "Response blowup or silent truncation", _build_ds3, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id ORDER BY i.id, t.thread_id NULLS LAST", _noop),
    DatasetFixture("DS-4", "Sparse nulls: tracking/thread/order_item", "Null handling through parse/load/merge", _build_ds4, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id ORDER BY i.id, t.thread_id NULLS LAST", _noop),
    DatasetFixture("DS-5", "Dirty text: unicode/emoji/control/4k subject", "Encoding and injection round-trip", _build_ds5, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id WHERE length(i.subject) >= 200 OR position('🚚' in i.subject) > 0 OR position('{\"incidents\":[]}' in i.subject) > 0 ORDER BY i.id, t.thread_id NULLS LAST", _noop),
    DatasetFixture("DS-6", "Boundary numerics incl 2^53±1 and negatives", "Numeric fidelity at incident grain", _build_ds6, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id WHERE i.id = ANY(%(ids)s) ORDER BY i.id, t.thread_id NULLS LAST", lambda s: _truth_ids(s, 5)),
    DatasetFixture("DS-7", "Collision scenarios on ids/threads and shared order ids", "Merge-key correctness", _build_ds7, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id WHERE i.order_id='OD-COLLIDE-1' ORDER BY i.id, t.thread_id NULLS LAST", _noop),
    DatasetFixture("DS-8", "Churn cohort present for cycle-1->cycle-2 mutation", "Re-collection reflects change", _build_ds8, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id WHERE i.id = ANY(%(ids)s) ORDER BY i.id, t.thread_id NULLS LAST", lambda s: _truth_ids(s, 200)),
    DatasetFixture("DS-9", "Late-arrival cohort baseline", "Mid-cycle insert catch-up on next cycle", _build_ds9, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id WHERE i.id = ANY(%(ids)s) ORDER BY i.id, t.thread_id NULLS LAST", lambda s: _truth_ids(s, 100)),
    DatasetFixture("DS-10", "Deletion cohort baseline", "Stale-row behavior after source deletions", _build_ds10, "SELECT i.id, t.thread_id FROM sentinel_incident i LEFT JOIN sentinel_thread t ON t.incident_id=i.id WHERE i.id = ANY(%(ids)s) ORDER BY i.id, t.thread_id NULLS LAST", lambda s: _truth_ids(s, 50)),
    DatasetFixture("DS-11", "Window contiguity 6-hour evenly spread", "Gap between caller windows", _build_ds11, "SELECT id AS incident_id FROM sentinel_incident ORDER BY id", _noop),
    DatasetFixture("DS-12", "Window boundary exact edges and ±1µs", "Off-by-one boundary loss/dup", _build_ds12, "SELECT id AS incident_id FROM sentinel_incident WHERE position('DS12-WINDOW-' in subject) = 1 ORDER BY id", _noop),
    DatasetFixture("DS-13", "Window overload 5,000 incidents in one hour", "Batch-cap paging correctness", _build_ds13, "SELECT id AS incident_id FROM sentinel_incident ORDER BY id", _noop),
    DatasetFixture("DS-14", "Mutation-in-window baseline", "Records moving in/out during cycle", _build_ds14, "SELECT id AS incident_id FROM sentinel_incident ORDER BY id", _noop),
    DatasetFixture("DS-15", "Empty and sparse windows baseline", "Zero and one-result windows semantics", _build_ds15, "SELECT id AS incident_id FROM sentinel_incident ORDER BY id", _noop),
]

