"""Generate realistic fake Flipkart Sentinel data into sentinel_mock.

Single seeding path for both the small demo dataset and production-like volume.

Precedence
----------
1. If ``N_INCIDENTS`` is set (non-empty), seed exactly that many incidents in one
   batch spread across ``SEED_START_DATE``..``SEED_END_DATE``. This is the quick
   demo override (historically ``N_INCIDENTS=1000``).
2. Otherwise use the date-range mode:
     SEED_START_DATE   default 2026-08-18
     SEED_END_DATE     default 2026-08-25
     SEED_MIN_PER_DAY  default 35000
     SEED_MAX_PER_DAY  default 40000
   One random count in [min, max] per calendar day (inclusive).

Performance: day-at-a-time generation + psycopg COPY (never executemany).
Threads for a day are written immediately after that day's incidents.
"""

from __future__ import annotations

import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from mock.reference import (
    AGING_SCORES,
    CHANNELS,
    HUMAN_USERS,
    ISSUE_IDS,
    NULL_TRACKING_RATE,
    QUEUE_NAME,
    SSI,
    STATUS,
    SYSTEM_USERS,
    THREAD_ENTRY_TYPES,
    THREADS_PER_INCIDENT,
    TRACKING_PREFIXES,
)

PRODUCT_FRAGMENTS = (
    "Noise Cancelling Headphones",
    "Cotton Casual Shirt",
    "Stainless Steel Bottle",
    "Wireless Mouse",
    "Running Shoes",
    "Kitchen Mixer Jar",
    "LED Desk Lamp",
    "Backpack 30L",
    "Smart Watch Band",
    "Phone Tempered Glass",
)

# Stable "as-of" for resolution_deadline_breach so re-seeds are identical.
BREACH_AS_OF = datetime(2026, 8, 25, 23, 59, 0, tzinfo=timezone.utc)

# Bytes/row rough estimate for capacity check (row + toast + indexes overhead).
_EST_BYTES_PER_INCIDENT = 900
_EST_BYTES_PER_THREAD = 350

DEFAULT_START = "2026-08-18"
DEFAULT_END = "2026-08-25"
DEFAULT_MIN_PER_DAY = 35_000
DEFAULT_MAX_PER_DAY = 40_000


def _load_dotenv() -> None:
    """Load .env into os.environ without overriding existing values."""
    env_path = Path(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'").strip('"')


def _parse_date(raw: str, name: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be YYYY-MM-DD, got {raw!r}") from exc


def incident_id(seq: int, when: datetime) -> str:
    """Global seq — never reset per day — so 300k ids cannot collide."""
    return "IN" + when.strftime("%y%m%d") + f"{seq:014d}"


def order_id(seq: int) -> str:
    n = (seq * 1_103_515_245 + 12_345_678_901_234_567) % (10**18)
    return f"OD{n:018d}"


def tracking_id(seq: int, prefix: str) -> str:
    digits = f"{(seq * 7_891 + 42) % (10**10):010d}"
    return f"{prefix}{digits}"


def _weighted_issue(rng: random.Random) -> tuple[str, int]:
    names = [name for name, _ in SSI]
    weights = [mult for _, mult in SSI]
    name = rng.choices(names, weights=weights, k=1)[0]
    return name, ISSUE_IDS[name]


def _weighted_status(rng: random.Random) -> tuple[int, str, str]:
    return rng.choices(list(STATUS), weights=[5, 3, 2], k=1)[0]


def _deadline_hours(rng: random.Random) -> int:
    return rng.choice((24, 48, 72))


def _business_hour_created(rng: random.Random, day: date) -> datetime:
    """created_at on ``day``, weighted toward IST business hours (≈09–18 IST).

    IST = UTC+5:30 → business window ≈ 03:30–12:30 UTC. Off-hours still get
    some volume so the day is not a hard clip.
    """
    # Hour weights in UTC: peak mid-morning–afternoon IST.
    hour_weights = [
        1, 1, 2, 4, 6, 8, 10, 12, 14, 12, 10, 8,  # 00–11 UTC
        6, 4, 3, 2, 2, 2, 2, 2, 1, 1, 1, 1,  # 12–23 UTC
    ]
    hour = rng.choices(range(24), weights=hour_weights, k=1)[0]
    minute = rng.randint(0, 59)
    second = rng.randint(0, 59)
    return datetime(
        day.year, day.month, day.day, hour, minute, second, tzinfo=timezone.utc
    )


def _daterange(start: date, end: date) -> list[date]:
    if end < start:
        raise SystemExit(f"SEED_END_DATE {end} is before SEED_START_DATE {start}")
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _null_and_prefix_plan(
    rng: random.Random, n: int
) -> tuple[list[bool], dict[int, str]]:
    null_flags = [rng.random() < NULL_TRACKING_RATE for _ in range(n)]
    target_nulls = max(1, round(n * NULL_TRACKING_RATE)) if n else 0
    current_nulls = sum(null_flags)
    if current_nulls < target_nulls:
        need = min(n, target_nulls - current_nulls)
        for i in rng.sample(range(n), k=need):
            null_flags[i] = True
    elif current_nulls > target_nulls:
        idxs = [i for i, flag in enumerate(null_flags) if flag]
        for i in rng.sample(idxs, k=current_nulls - target_nulls):
            null_flags[i] = False

    non_null_idxs = [i for i, flag in enumerate(null_flags) if not flag]
    prefix_by_idx: dict[int, str] = {}
    for i, prefix in enumerate(TRACKING_PREFIXES):
        if i < len(non_null_idxs):
            prefix_by_idx[non_null_idxs[i]] = prefix
    for i in non_null_idxs:
        if i not in prefix_by_idx:
            prefix_by_idx[i] = rng.choice(TRACKING_PREFIXES)
    return null_flags, prefix_by_idx


def _updated_on(
    rng: random.Random,
    created_at: datetime,
    day: date,
    range_end: date,
) -> datetime:
    """~70% same calendar day as created; ~30% a later day within the range."""
    if rng.random() < 0.70 or day >= range_end:
        # Same day, at or after created_at.
        day_end = datetime(
            day.year, day.month, day.day, 23, 59, 59, tzinfo=timezone.utc
        )
        span = max(1, int((day_end - created_at).total_seconds()))
        return created_at + timedelta(seconds=rng.randint(0, span))

    later_days = [
        day + timedelta(days=d)
        for d in range(1, (range_end - day).days + 1)
    ]
    upd_day = rng.choice(later_days)
    # Later day: any time that day (still weighted slightly to business hours).
    upd = _business_hour_created(rng, upd_day)
    if upd < created_at:
        upd = created_at + timedelta(minutes=rng.randint(1, 180))
    return upd


def _thread_created(
    rng: random.Random,
    created_at: datetime,
    updated_on: datetime,
    t_i: int,
    n_threads: int,
) -> datetime:
    span = max(1, int((updated_on - created_at).total_seconds()))
    # Spread threads across [created_at, updated_on].
    offset = int(span * (t_i + 1) / (n_threads + 1))
    jitter = rng.randint(0, max(1, span // max(n_threads, 1)))
    t = created_at + timedelta(seconds=min(span, offset + jitter))
    if t < created_at:
        t = created_at + timedelta(seconds=t_i + 1)
    if t > updated_on:
        t = updated_on
    return t


def build_day_rows(
    *,
    day: date,
    n_incidents: int,
    seq_start: int,
    thread_seq_start: int,
    range_end: date,
    rng: random.Random,
) -> tuple[list[tuple], list[tuple], int, int]:
    """Build incident + thread rows for one calendar day.

    Returns (incidents, threads, next_seq, next_thread_seq).
    """
    lo, hi = THREADS_PER_INCIDENT
    null_flags, prefix_by_idx = _null_and_prefix_plan(rng, n_incidents)
    incidents: list[tuple] = []
    threads: list[tuple] = []
    seq = seq_start
    thread_seq = thread_seq_start

    for local_i in range(n_incidents):
        seq += 1
        created_at = _business_hour_created(rng, day)
        updated_on = _updated_on(rng, created_at, day, range_end)
        issue_name, issue_id = _weighted_issue(rng)
        status_id, status_status, status_status_type = _weighted_status(rng)
        deadline = created_at + timedelta(hours=_deadline_hours(rng))
        product = rng.choice(PRODUCT_FRAGMENTS)
        subject = f"Update on your order for {product}"
        tid = (
            None
            if null_flags[local_i]
            else tracking_id(seq, prefix_by_idx[local_i])
        )
        iid = incident_id(seq, created_at)
        oid = order_id(seq)

        incidents.append(
            (
                iid,
                issue_id,
                issue_name,
                None,
                None,
                None,
                None,
                oid,
                str(4_000_000_000_000_000 + seq),
                str(5_000_000_000_000_000 + seq),
                tid,
                f"FSN{seq:012d}",
                rng.randint(1, 100),
                deadline,
                deadline < BREACH_AS_OF,
                deadline + timedelta(hours=24) if rng.random() < 0.2 else None,
                f"SL{seq:010d}",
                "Sentinel",
                status_id,
                status_status,
                status_status_type,
                subject,
                updated_on,
                rng.choice(AGING_SCORES),
                rng.choice(SYSTEM_USERS),
                None,
                None,
                None,
                None,
                QUEUE_NAME,
                rng.choice(HUMAN_USERS),
                created_at,
            )
        )

        n_threads = rng.randint(lo, hi)
        for t_i in range(n_threads):
            thread_seq += 1
            entry_id, entry_name = rng.choice(THREAD_ENTRY_TYPES)
            channel_id, channel_name = rng.choice(CHANNELS)
            t_created = _thread_created(
                rng, created_at, updated_on, t_i, n_threads
            )
            t_updated = min(
                updated_on,
                t_created + timedelta(minutes=rng.randint(0, 30)),
            )
            system_thread = entry_id in (6, 30, 1005)
            created_by = (
                rng.choice(SYSTEM_USERS)
                if system_thread
                else rng.choice(HUMAN_USERS)
            )
            threads.append(
                (
                    f"TH{thread_seq:018d}",
                    iid,
                    channel_id,
                    channel_name,
                    str(9_000_000_000_000 + thread_seq),
                    "text/plain",
                    t_created,
                    created_by,
                    system_thread,
                    entry_id,
                    entry_name,
                    t_updated,
                    created_by,
                )
            )

    return incidents, threads, seq, thread_seq


INCIDENT_COLS = (
    "id, issue_id, issue_name, issue_parent_id, issue_parent_name, "
    "issue_grandparent_id, issue_grandparent_name, order_id, order_item_id, "
    "order_item_unit_id, tracking_id, order_item_product_fsn, incident_score, "
    "resolution_deadline, resolution_deadline_breach, resolution_re_deadline, "
    "seller_id, source, status_id, status_status, status_status_type, subject, "
    "updated_on, aging_score, last_updated_by_user, payment_id, booking_id, "
    "reverse_tracking_id, return_id, queue, assigned_to, created_at"
)

THREAD_COLS = (
    "thread_id, incident_id, channel_id, channel_name, communication_id, "
    "content_type, created_at, created_by, system_thread, thread_entry_type_id, "
    "thread_entry_type_name, updated_at, updated_by"
)

THREAD_INCIDENT_INDEX = "idx_sentinel_thread_incident_id"


def _plan_days(
    *,
    n_override: int | None,
    start: date,
    end: date,
    min_per_day: int,
    max_per_day: int,
    rng: random.Random,
) -> list[tuple[date, int]]:
    days = _daterange(start, end)
    if n_override is not None:
        if n_override < 0:
            raise SystemExit("N_INCIDENTS must be >= 0")
        # Spread the small batch across days as evenly as possible.
        base, rem = divmod(n_override, len(days))
        return [
            (d, base + (1 if i < rem else 0)) for i, d in enumerate(days)
        ]
    if min_per_day > max_per_day:
        raise SystemExit("SEED_MIN_PER_DAY must be <= SEED_MAX_PER_DAY")
    return [(d, rng.randint(min_per_day, max_per_day)) for d in days]


def _capacity_check(plan: list[tuple[date, int]], dsn: str) -> None:
    n_inc = sum(n for _, n in plan)
    # Midpoint of 1–4 threads ≈ 2.5
    n_thr_est = int(n_inc * 2.5)
    est_bytes = n_inc * _EST_BYTES_PER_INCIDENT + n_thr_est * _EST_BYTES_PER_THREAD
    est_gb = est_bytes / (1024**3)
    print("=== capacity check (before load) ===")
    print(f"planned incidents: {n_inc:,}")
    print(f"estimated threads (~2.5x): {n_thr_est:,}")
    print(
        f"estimated on-disk (row+index rough): {est_gb:.2f} GiB "
        f"({est_bytes:,} bytes)"
    )
    print("Cloud SQL allocation: 20 GiB (storage-auto-increase enabled)")
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pg_database_size(current_database()) AS db_bytes,
                           pg_size_pretty(pg_database_size(current_database()))
                             AS db_pretty
                    """
                )
                db_bytes, db_pretty = cur.fetchone()
                print(f"current database size: {db_pretty} ({db_bytes:,} bytes)")
    except Exception as exc:  # noqa: BLE001
        print(f"current database size: (unavailable: {exc})")
    if est_gb > 15:
        print(
            "WARNING: estimate is large relative to 20 GiB; "
            "auto-increase should cover growth"
        )


def _drop_thread_incident_index(cur: psycopg.Cursor) -> None:
    # Maintaining idx_sentinel_thread_incident_id across ~750k inserts is
    # slower than dropping it for the load and rebuilding once afterwards.
    cur.execute(f"DROP INDEX IF EXISTS {THREAD_INCIDENT_INDEX}")


def _recreate_thread_incident_index(cur: psycopg.Cursor) -> None:
    cur.execute(
        f"CREATE INDEX IF NOT EXISTS {THREAD_INCIDENT_INDEX} "
        f"ON sentinel_thread (incident_id)"
    )


def seed(conn: psycopg.Connection, plan: list[tuple[date, int]]) -> float:
    """Load plan day-by-day via COPY. Returns wall-clock seconds."""
    rng = random.Random(20260803)  # stable across demo rehearsals
    range_end = plan[-1][0]
    seq = 0
    thread_seq = 0
    t0 = time.perf_counter()

    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE sentinel_thread, sentinel_incident "
            "RESTART IDENTITY CASCADE"
        )
        _drop_thread_incident_index(cur)
    conn.commit()

    for day, n in plan:
        if n == 0:
            print(f"  {day.isoformat()}: incidents=0 threads=0 elapsed=0.00s (skip)")
            continue
        day_t0 = time.perf_counter()
        incidents, threads, seq, thread_seq = build_day_rows(
            day=day,
            n_incidents=n,
            seq_start=seq,
            thread_seq_start=thread_seq,
            range_end=range_end,
            rng=rng,
        )
        with conn.cursor() as cur:
            with cur.copy(
                f"COPY sentinel_incident ({INCIDENT_COLS}) FROM STDIN"
            ) as copy:
                for row in incidents:
                    copy.write_row(row)
            with cur.copy(
                f"COPY sentinel_thread ({THREAD_COLS}) FROM STDIN"
            ) as copy:
                for row in threads:
                    copy.write_row(row)
        conn.commit()
        elapsed = time.perf_counter() - day_t0
        print(
            f"  {day.isoformat()}: incidents={len(incidents):,} "
            f"threads={len(threads):,} elapsed={elapsed:.2f}s"
        )

    with conn.cursor() as cur:
        _recreate_thread_incident_index(cur)
        cur.execute("ANALYZE sentinel_incident")
        cur.execute("ANALYZE sentinel_thread")
    conn.commit()

    total = time.perf_counter() - t0
    return total


def verify(
    conn: psycopg.Connection,
    *,
    expected_range: tuple[date, date] | None = None,
    min_per_day: int | None = None,
    max_per_day: int | None = None,
) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM sentinel_incident")
        n_inc = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM sentinel_thread")
        n_thr = cur.fetchone()["n"]
        factor = (n_thr / n_inc) if n_inc else 0.0

        cur.execute("SELECT count(DISTINCT id) AS n FROM sentinel_incident")
        n_distinct = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT date(created_at AT TIME ZONE 'UTC') AS d,
                   count(*) AS incidents
            FROM sentinel_incident
            GROUP BY 1
            ORDER BY 1
            """
        )
        per_day_inc = {row["d"]: row["incidents"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT date(i.created_at AT TIME ZONE 'UTC') AS d,
                   count(*) AS threads
            FROM sentinel_thread t
            JOIN sentinel_incident i ON i.id = t.incident_id
            GROUP BY 1
            ORDER BY 1
            """
        )
        per_day_thr = {row["d"]: row["threads"] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT min(created_at) AS min_c, max(created_at) AS max_c,
                   min(updated_on) AS min_u, max(updated_on) AS max_u
            FROM sentinel_incident
            """
        )
        bounds = cur.fetchone()

        cur.execute(
            "SELECT count(*) AS n FROM sentinel_incident WHERE tracking_id IS NULL"
        )
        n_null = cur.fetchone()["n"]
        null_pct = (100.0 * n_null / n_inc) if n_inc else 0.0

        cur.execute(
            """
            SELECT left(tracking_id, 4) AS prefix, count(*) AS n
            FROM sentinel_incident
            WHERE tracking_id IS NOT NULL
            GROUP BY 1
            ORDER BY 1
            """
        )
        prefixes = cur.fetchall()

        cur.execute(
            """
            SELECT issue_name, count(*) AS n
            FROM sentinel_incident
            GROUP BY issue_name
            ORDER BY n DESC, issue_name
            """
        )
        issues = cur.fetchall()

        cur.execute(
            """
            SELECT status_status, count(*) AS n
            FROM sentinel_incident
            GROUP BY status_status
            ORDER BY n DESC, status_status
            """
        )
        statuses = cur.fetchall()

        cur.execute(
            """
            SELECT id
            FROM sentinel_incident
            ORDER BY (
              SELECT count(*) FROM sentinel_thread t
              WHERE t.incident_id = sentinel_incident.id
            ) DESC, id
            LIMIT 1
            """
        )
        sample = cur.fetchone()
        sample_id = sample["id"] if sample else None
        sample_threads: list[dict] = []
        if sample_id:
            cur.execute(
                """
                SELECT thread_id, incident_id, thread_entry_type_id,
                       thread_entry_type_name, created_at, system_thread
                FROM sentinel_thread
                WHERE incident_id = %s
                ORDER BY created_at, thread_id
                """,
                (sample_id,),
            )
            sample_threads = list(cur.fetchall())
            cur.execute(
                "SELECT * FROM sentinel_incident WHERE id = %s", (sample_id,)
            )
            sample_inc = cur.fetchone()
        else:
            sample_inc = None

        # Last 24h of the requested range (or of the data if no range given).
        if expected_range is not None:
            window_end = datetime(
                expected_range[1].year,
                expected_range[1].month,
                expected_range[1].day,
                23,
                59,
                59,
                tzinfo=timezone.utc,
            )
        else:
            window_end = bounds["max_u"]
        window_start = window_end - timedelta(hours=24)
        cur.execute(
            """
            SELECT count(*) AS n FROM sentinel_incident
            WHERE updated_on >= %s AND updated_on <= %s
            """,
            (window_start, window_end),
        )
        n_last_24h = cur.fetchone()["n"]

        cur.execute(
            """
            SELECT
              pg_size_pretty(pg_total_relation_size('sentinel_incident'))
                AS incident_size,
              pg_total_relation_size('sentinel_incident') AS incident_bytes,
              pg_size_pretty(pg_total_relation_size('sentinel_thread'))
                AS thread_size,
              pg_total_relation_size('sentinel_thread') AS thread_bytes,
              pg_size_pretty(pg_database_size(current_database())) AS db_size
            """
        )
        sizes = cur.fetchone()

    print("=== sentinel_mock verification ===")
    print(f"total incidents: {n_inc:,}")
    print(f"total threads:   {n_thr:,}")
    print(f"achieved explosion factor: {factor:.3f}")
    print(
        f"real export dumps for reference: 334,879 incidents / 797,374 threads "
        f"(factor {797374 / 334879:.3f})"
    )
    print("per-day counts:")
    print(f"  {'date':<12} {'incidents':>10} {'threads':>10}")
    all_days = sorted(set(per_day_inc) | set(per_day_thr))
    for d in all_days:
        print(
            f"  {d.isoformat():<12} {per_day_inc.get(d, 0):>10,} "
            f"{per_day_thr.get(d, 0):>10,}"
        )

    print(
        f"created_at range: {bounds['min_c']} .. {bounds['max_c']}"
    )
    print(
        f"updated_on range: {bounds['min_u']} .. {bounds['max_u']}"
    )
    if expected_range is not None:
        lo, hi = expected_range
        lo_dt = datetime(lo.year, lo.month, lo.day, tzinfo=timezone.utc)
        hi_dt = datetime(hi.year, hi.month, hi.day, 23, 59, 59, tzinfo=timezone.utc)
        ok_c = bounds["min_c"] >= lo_dt and bounds["max_c"] <= hi_dt
        ok_u = bounds["min_u"] >= lo_dt and bounds["max_u"] <= hi_dt
        print(f"dates inside [{lo} .. {hi}] UTC: created_at={ok_c} updated_on={ok_u}")
        if not ok_c or not ok_u:
            raise SystemExit("FAIL: timestamps outside requested range")

    if min_per_day is not None and max_per_day is not None:
        bad = [
            (d, n)
            for d, n in per_day_inc.items()
            if not (min_per_day <= n <= max_per_day)
        ]
        if bad:
            raise SystemExit(
                f"FAIL: per-day counts outside [{min_per_day}, {max_per_day}]: {bad}"
            )
        print(
            f"per-day incidents all within [{min_per_day:,}, {max_per_day:,}]: OK"
        )

    print(f"NULL tracking_id: {n_null:,} ({null_pct:.1f}%) (expect ~14%)")
    print("FMP prefixes:")
    for row in prefixes:
        print(f"  {row['prefix']}: {row['n']:,}")
    present = {row["prefix"] for row in prefixes}
    missing = [p for p in TRACKING_PREFIXES if p not in present]
    if missing:
        raise SystemExit(f"FAIL: missing tracking prefixes {missing}")
    print("issue_name counts (desc):")
    for row in issues:
        print(f"  {row['issue_name']}: {row['n']:,}")
    print("status counts:")
    for row in statuses:
        print(f"  {row['status_status']}: {row['n']:,}")

    print(f"distinct incident ids: {n_distinct:,} (rows={n_inc:,})")
    if n_distinct != n_inc:
        raise SystemExit(
            f"FAIL: distinct id count {n_distinct} != row count {n_inc}"
        )
    print("distinct id count equals row count: OK")

    print(f"sample incident (max threads): {sample_id}")
    if sample_inc:
        print(
            f"  created_at={sample_inc['created_at']} "
            f"updated_on={sample_inc['updated_on']} "
            f"issue={sample_inc['issue_name']} "
            f"status={sample_inc['status_status']}"
        )
    for thr in sample_threads:
        print(
            f"  thread_id={thr['thread_id']} type={thr['thread_entry_type_id']}:"
            f"{thr['thread_entry_type_name']} system={thr['system_thread']} "
            f"created_at={thr['created_at']}"
        )

    print(
        f"incidents with updated_on in last 24h of range "
        f"[{window_start} .. {window_end}]: {n_last_24h:,}"
    )
    print(
        f"table sizes: sentinel_incident={sizes['incident_size']} "
        f"({sizes['incident_bytes']:,} B), "
        f"sentinel_thread={sizes['thread_size']} "
        f"({sizes['thread_bytes']:,} B), db={sizes['db_size']}"
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verify_only = "--verify-only" in argv

    _load_dotenv()
    dsn = os.environ.get("SENTINEL_MOCK_DSN")
    if not dsn:
        print("SENTINEL_MOCK_DSN is required", file=sys.stderr)
        return 1

    start = _parse_date(
        os.environ.get("SEED_START_DATE", DEFAULT_START), "SEED_START_DATE"
    )
    end = _parse_date(
        os.environ.get("SEED_END_DATE", DEFAULT_END), "SEED_END_DATE"
    )
    min_per_day = int(os.environ.get("SEED_MIN_PER_DAY", str(DEFAULT_MIN_PER_DAY)))
    max_per_day = int(os.environ.get("SEED_MAX_PER_DAY", str(DEFAULT_MAX_PER_DAY)))

    n_raw = os.environ.get("N_INCIDENTS", "").strip()
    n_override: int | None
    if n_raw:
        try:
            n_override = int(n_raw)
        except ValueError:
            print(f"N_INCIDENTS must be an int, got {n_raw!r}", file=sys.stderr)
            return 1
        print(
            f"mode=N_INCIDENTS override ({n_override}) across "
            f"{start}..{end}"
        )
    else:
        n_override = None
        print(
            f"mode=date-range {start}..{end} "
            f"per_day=[{min_per_day}, {max_per_day}]"
        )

    rng = random.Random(20260803)
    plan = _plan_days(
        n_override=n_override,
        start=start,
        end=end,
        min_per_day=min_per_day,
        max_per_day=max_per_day,
        rng=rng,
    )

    with psycopg.connect(dsn) as conn:
        if not verify_only:
            _capacity_check(plan, dsn)
            print("=== loading (one day at a time, COPY) ===")
            elapsed = seed(conn, plan)
            print(f"seed wall-clock: {elapsed:.1f}s")
            if elapsed > 300:
                print(
                    f"WARNING: seed took {elapsed:.1f}s (>5 min target); "
                    "investigate COPY/index strategy before re-running larger sets"
                )
            else:
                print("seed finished under 5-minute target")

        # Enforce per-day band only in full date-range mode (not N_INCIDENTS).
        verify(
            conn,
            expected_range=(start, end),
            min_per_day=None if n_override is not None else min_per_day,
            max_per_day=None if n_override is not None else max_per_day,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
