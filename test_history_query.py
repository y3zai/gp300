import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.history import build_history_query


HISTORY_COLUMNS = (
    "timestamp, value, num_constituents, weighted_entropy, divisor"
)


def create_history_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        f"""
        CREATE TABLE history (
            timestamp TEXT PRIMARY KEY,
            value REAL NOT NULL,
            num_constituents INTEGER NOT NULL,
            weighted_entropy REAL NOT NULL,
            divisor REAL NOT NULL
        )
        """
    )
    return conn


def insert_history(conn, timestamp, value):
    conn.execute(
        f"INSERT INTO history ({HISTORY_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
        (timestamp.isoformat(), value, 300, 0.5, 0.001),
    )


def run_history_query(conn, *, since=None, now):
    sql, params = build_history_query(since=since, now=now)
    return conn.execute(sql, params).fetchall()


def test_history_query_keeps_every_recent_point():
    now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
    conn = create_history_db()
    try:
        timestamps = [
            now - timedelta(minutes=10),
            now - timedelta(minutes=5),
            now,
        ]
        for index, timestamp in enumerate(timestamps):
            insert_history(conn, timestamp, index)

        rows = run_history_query(conn, now=now)

        assert [row[0] for row in rows] == [
            timestamp.isoformat() for timestamp in timestamps
        ]
    finally:
        conn.close()


def test_history_query_keeps_latest_point_per_hour_after_seven_days():
    now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
    conn = create_history_db()
    try:
        timestamps = [
            now - timedelta(days=8, minutes=50),
            now - timedelta(days=8, minutes=10),
            now - timedelta(days=8) + timedelta(hours=1),
        ]
        for index, timestamp in enumerate(timestamps):
            insert_history(conn, timestamp, index)

        rows = run_history_query(conn, now=now)

        assert [row[0] for row in rows] == [
            timestamps[1].isoformat(),
            timestamps[2].isoformat(),
        ]
    finally:
        conn.close()


def test_history_query_keeps_latest_point_per_day_after_one_year():
    now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
    conn = create_history_db()
    try:
        timestamps = [
            now - timedelta(days=400, hours=2),
            now - timedelta(days=400, hours=1),
            now - timedelta(days=399),
        ]
        for index, timestamp in enumerate(timestamps):
            insert_history(conn, timestamp, index)

        rows = run_history_query(conn, now=now)

        assert [row[0] for row in rows] == [
            timestamps[1].isoformat(),
            timestamps[2].isoformat(),
        ]
    finally:
        conn.close()


def test_history_query_applies_since_before_sampling():
    now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
    conn = create_history_db()
    try:
        before_since = now - timedelta(days=10, minutes=30)
        since = now - timedelta(days=10, minutes=15)
        after_since = now - timedelta(days=10, minutes=5)
        insert_history(conn, before_since, 1)
        insert_history(conn, after_since, 2)

        rows = run_history_query(conn, since=since.isoformat(), now=now)

        assert [row[0] for row in rows] == [after_since.isoformat()]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "since",
    [
        "not-a-timestamp",
        "2026-07-28T04:00:00",
    ],
)
def test_history_query_rejects_invalid_since(since):
    now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="since"):
        build_history_query(since=since, now=now)


def test_history_payloads_match_live_sampling_at_partial_boundaries():
    from src.history import build_history_payload_query

    now = datetime(2026, 11, 4, 15, 25, tzinfo=timezone.utc)
    migration = Path(__file__).with_name("migrations") / "0001_history_payloads.sql"
    conn = create_history_db()
    try:
        timestamps = [now - timedelta(days=1), now]
        for boundary in [now - timedelta(days=7), now - timedelta(days=365)]:
            timestamps.extend(boundary + timedelta(minutes=delta) for delta in (
                -1500, -60, -10, -5, 0, 5, 60, 1500
            ))
        for index, timestamp in enumerate(reversed(timestamps)):
            insert_history(conn, timestamp, index)

        conn.executescript(migration.read_text())
        # An older observation can arrive after the migration and change an
        # already materialized day and hour.
        insert_history(conn, now - timedelta(days=30, minutes=2), 123)
        insert_history(conn, now - timedelta(days=30, minutes=1), 124)
        insert_history(conn, now - timedelta(days=1, minutes=1), 125)

        for since in [None, *(ts.isoformat() for ts in timestamps)]:
            query, params = build_history_payload_query(since=since, now=now)
            actual = [
                tuple(point[column] for column in HISTORY_COLUMNS.split(", "))
                for (payload,) in conn.execute(query, params)
                for point in json.loads(payload)
            ]
            assert actual == run_history_query(conn, since=since, now=now)
    finally:
        conn.close()
