"""Generate realistic fake Flipkart Sentinel data into sentinel_mock.

Re-runnable: truncates sentinel_thread and sentinel_incident, then bulk-inserts.
Reads SENTINEL_MOCK_DSN and N_INCIDENTS from the environment.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone
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


def incident_id(seq: int, when: datetime) -> str:
    return "IN" + when.strftime("%y%m%d") + f"{seq:014d}"


def order_id(seq: int) -> str:
    # 18 digits, varied (not a plain counter): mix seq with a large odd multiplier.
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
    # Roughly 5:3:2 toward Solved (STATUS order: Solved, Unresolved, Updated).
    return rng.choices(list(STATUS), weights=[5, 3, 2], k=1)[0]


def _deadline_hours(rng: random.Random) -> int:
    return rng.choice((24, 48, 72))


def build_rows(
    n_incidents: int,
    *,
    rng: random.Random,
    now: datetime,
) -> tuple[list[tuple], list[tuple]]:
    lo, hi = THREADS_PER_INCIDENT
    incidents: list[tuple] = []
    threads: list[tuple] = []

    # Pre-assign null-tracking and prefixes so the three Sentinel properties hold.
    null_flags = [rng.random() < NULL_TRACKING_RATE for _ in range(n_incidents)]
    # Guarantee ~NULL_TRACKING_RATE and at least some nulls when n is large.
    target_nulls = max(1, round(n_incidents * NULL_TRACKING_RATE)) if n_incidents else 0
    current_nulls = sum(null_flags)
    if current_nulls < target_nulls:
        for i in rng.sample(range(n_incidents), k=min(n_incidents, target_nulls - current_nulls)):
            null_flags[i] = True
    elif current_nulls > target_nulls and target_nulls >= 0:
        idxs = [i for i, flag in enumerate(null_flags) if flag]
        for i in rng.sample(idxs, k=current_nulls - target_nulls):
            null_flags[i] = False

    non_null_idxs = [i for i, flag in enumerate(null_flags) if not flag]
    prefix_by_idx: dict[int, str] = {}
    # Force all three OBSERVED prefixes to appear among non-null tracking ids.
    for i, prefix in enumerate(TRACKING_PREFIXES):
        if i < len(non_null_idxs):
            prefix_by_idx[non_null_idxs[i]] = prefix
    for i in non_null_idxs:
        if i not in prefix_by_idx:
            prefix_by_idx[i] = rng.choice(TRACKING_PREFIXES)

    thread_seq = 0
    for seq in range(1, n_incidents + 1):
        idx = seq - 1
        created_at = now - timedelta(seconds=rng.randint(0, 72 * 3600))
        issue_name, issue_id = _weighted_issue(rng)
        status_id, status_status, status_status_type = _weighted_status(rng)
        deadline = created_at + timedelta(hours=_deadline_hours(rng))
        product = rng.choice(PRODUCT_FRAGMENTS)
        subject = f"Update on your order for {product}"
        tid = None if null_flags[idx] else tracking_id(seq, prefix_by_idx[idx])

        iid = incident_id(seq, created_at)
        oid = order_id(seq)
        updated_on = created_at + timedelta(minutes=rng.randint(5, 24 * 60))
        if updated_on > now:
            updated_on = now

        incidents.append(
            (
                iid,
                issue_id,
                issue_name,
                None,  # issue_parent_id
                None,  # issue_parent_name
                None,  # issue_grandparent_id
                None,  # issue_grandparent_name
                oid,
                float(4_000_000_000_000_000 + seq),  # order_item_id
                float(5_000_000_000_000_000 + seq),  # order_item_unit_id
                tid,
                f"FSN{seq:012d}",
                rng.randint(1, 100),  # incident_score
                deadline,
                deadline < now,  # resolution_deadline_breach
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
                None,  # payment_id
                None,  # booking_id
                None,  # reverse_tracking_id
                None,  # return_id
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
            t_created = created_at + timedelta(minutes=rng.randint(1, max(2, (t_i + 1) * 45)))
            if t_created > now:
                t_created = now
            if t_created <= created_at:
                t_created = created_at + timedelta(seconds=t_i + 1)
            t_updated = t_created + timedelta(minutes=rng.randint(0, 30))
            if t_updated > now:
                t_updated = now
            system_thread = entry_id in (6, 30, 1005)
            created_by = (
                rng.choice(SYSTEM_USERS) if system_thread else rng.choice(HUMAN_USERS)
            )
            threads.append(
                (
                    f"TH{thread_seq:018d}",
                    iid,
                    channel_id,
                    channel_name,
                    float(9_000_000_000_000 + thread_seq),
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

    return incidents, threads


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


def seed(conn: psycopg.Connection, n_incidents: int) -> None:
    rng = random.Random(20260803)  # stable across demo rehearsals
    now = datetime.now(timezone.utc)
    incidents, threads = build_rows(n_incidents, rng=rng, now=now)

    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE TABLE sentinel_thread, sentinel_incident RESTART IDENTITY CASCADE"
        )
        with cur.copy(f"COPY sentinel_incident ({INCIDENT_COLS}) FROM STDIN") as copy:
            for row in incidents:
                copy.write_row(row)
        with cur.copy(f"COPY sentinel_thread ({THREAD_COLS}) FROM STDIN") as copy:
            for row in threads:
                copy.write_row(row)
    conn.commit()


def verify(conn: psycopg.Connection, expected_incidents: int | None = None) -> None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM sentinel_incident")
        n_inc = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM sentinel_thread")
        n_thr = cur.fetchone()["n"]
        factor = (n_thr / n_inc) if n_inc else 0.0

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
            SELECT id
            FROM sentinel_incident
            ORDER BY (
              SELECT count(*) FROM sentinel_thread t WHERE t.incident_id = sentinel_incident.id
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

    print("=== sentinel_mock verification ===")
    if expected_incidents is not None:
        print(f"incidents: {n_inc} (expect {expected_incidents})")
    else:
        print(f"incidents: {n_inc}")
    print(f"threads:   {n_thr} (expect roughly 2.5x incidents)")
    print(f"thread explosion factor: {factor:.3f}")
    print(f"NULL tracking_id: {n_null} ({null_pct:.1f}%) (expect ~14%)")
    print("tracking prefixes:")
    present = {row["prefix"] for row in prefixes}
    for row in prefixes:
        print(f"  {row['prefix']}: {row['n']}")
    missing = [p for p in TRACKING_PREFIXES if p not in present]
    if missing:
        print(f"  MISSING prefixes: {missing}")
    else:
        print("  all three prefixes present (FMPC/FMPP/FMPN)")
    print("issue_name counts (desc):")
    for row in issues:
        print(f"  {row['issue_name']}: {row['n']}")
    print(f"sample incident (thread explosion): {sample_id}")
    for thr in sample_threads:
        print(
            f"  thread_id={thr['thread_id']} type={thr['thread_entry_type_id']}:"
            f"{thr['thread_entry_type_name']} system={thr['system_thread']} "
            f"created_at={thr['created_at']}"
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verify_only = "--verify-only" in argv

    _load_dotenv()
    dsn = os.environ.get("SENTINEL_MOCK_DSN")
    if not dsn:
        print("SENTINEL_MOCK_DSN is required", file=sys.stderr)
        return 1
    n_raw = os.environ.get("N_INCIDENTS", "1000")
    try:
        n_incidents = int(n_raw)
    except ValueError:
        print(f"N_INCIDENTS must be an int, got {n_raw!r}", file=sys.stderr)
        return 1

    with psycopg.connect(dsn) as conn:
        if not verify_only:
            seed(conn, n_incidents)
            print(f"Seeded {n_incidents} incidents into sentinel_mock")
        verify(conn, expected_incidents=None if verify_only else n_incidents)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
