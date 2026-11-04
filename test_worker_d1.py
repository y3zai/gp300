import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.d1_state import STATE_QUERY, build_d1_update, decode_state, select_update
from src.engine import Constituent, IndexState


def save_results(connection, state, previous):
    slot = state.timestamp.replace(second=0, microsecond=0)
    mode = select_update(previous, slot)
    with connection:
        for sql, params in build_d1_update(state, previous, slot, mode):
            connection.execute(sql, params)
    return decode_state(connection.execute(STATE_QUERY).fetchone()[0])


def test_save_results_writes_only_changed_constituents():
    timestamp = datetime(2026, 11, 4, tzinfo=timezone.utc)
    unchanged = Constituent(
        id="same", label="Will it happen?", source_type="market",
        num_outcomes=2, probabilities=[0.54321, 0.45679], volume_1mo=101.234,
        end_date=None, weight=0.5, rank=1, normalized_entropy=0.75,
    )
    changed = replace(unchanged, id="change", rank=2)
    removed = replace(unchanged, id="remove", rank=3)
    added = replace(unchanged, id="add", rank=3)
    before = IndexState(
        value=1000, divisor=0.005, weighted_entropy=0.5,
        num_constituents=3, timestamp=timestamp,
        constituents=[unchanged, changed, removed],
    )
    after = replace(
        before,
        timestamp=timestamp + timedelta(minutes=5),
        constituents=[unchanged, replace(changed, end_date=timestamp), added],
    )

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(Path(__file__).with_name("schema.sql").read_text())
        previous = save_results(connection, before, None)
        connection.executescript("""
            CREATE TABLE writes (operation TEXT, id TEXT);
            CREATE TRIGGER log_insert AFTER INSERT ON constituents BEGIN
                INSERT INTO writes VALUES ('insert', NEW.id);
            END;
            CREATE TRIGGER log_update AFTER UPDATE ON constituents BEGIN
                INSERT INTO writes VALUES ('update', NEW.id);
            END;
            CREATE TRIGGER log_delete AFTER DELETE ON constituents BEGIN
                INSERT INTO writes VALUES ('delete', OLD.id);
            END;
        """)

        previous = save_results(connection, after, previous)

        assert connection.execute(
            "SELECT operation, id FROM writes ORDER BY operation"
        ).fetchall() == [("delete", "remove"), ("insert", "add"), ("update", "change")]
        assert connection.execute(
            "SELECT id, probabilities, volume_1mo, end_date, rank FROM constituents ORDER BY rank"
        ).fetchall() == [
            ("same", "[0.5432, 0.4568]", 101.23, None, 1),
            ("change", "[0.5432, 0.4568]", 101.23, timestamp.isoformat(), 2),
            ("add", "[0.5432, 0.4568]", 101.23, None, 3),
        ]

        connection.execute("DELETE FROM writes")
        unchanged = replace(after, timestamp=timestamp + timedelta(minutes=10))
        previous = save_results(connection, unchanged, previous)
        assert connection.execute("SELECT * FROM writes").fetchall() == []

        empty = replace(after, timestamp=timestamp + timedelta(minutes=15), num_constituents=0, constituents=[])
        save_results(connection, empty, previous)
        assert connection.execute("SELECT * FROM constituents").fetchall() == []
