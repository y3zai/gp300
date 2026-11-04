import asyncio
import json
import runpy
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parent


class LocalStatement:
    def __init__(self, sql, params=()):
        self.sql = sql
        self.params = params

    def bind(self, *params):
        return LocalStatement(self.sql, params)


class LocalD1:
    def __init__(self, connection):
        self.connection = connection

    def prepare(self, sql):
        return LocalStatement(sql)

    async def batch(self, statements):
        with self.connection:
            for statement in statements:
                self.connection.execute(statement.sql, statement.params)


def test_save_results_writes_only_changed_constituents(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "src"))
    monkeypatch.setitem(sys.modules, "js", SimpleNamespace(
        JSON=SimpleNamespace(parse=json.loads), console=None, fetch=None,
    ))
    monkeypatch.setitem(sys.modules, "workers", SimpleNamespace(
        Response=None, WorkerEntrypoint=object,
    ))
    worker = runpy.run_path(str(ROOT / "src" / "worker.py"))
    save_results = worker["save_results_d1"]
    timestamp = datetime(2026, 11, 4, tzinfo=timezone.utc)
    unchanged = worker["Constituent"](
        id="same", label="Will it happen?", source_type="market",
        num_outcomes=2, probabilities=[0.54321, 0.45679], volume_1mo=101.234,
        end_date=None, weight=0.5, rank=1, normalized_entropy=0.75,
    )
    changed = replace(unchanged, id="change", rank=2)
    removed = replace(unchanged, id="remove", rank=3)
    added = replace(unchanged, id="add", rank=3)
    before = worker["IndexState"](
        value=1000, divisor=0.005, weighted_entropy=0.5,
        num_constituents=3, timestamp=timestamp,
        constituents=[unchanged, changed, removed],
    )
    after = replace(before, constituents=[
        unchanged, replace(changed, end_date=timestamp), added,
    ])

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript((ROOT / "schema.sql").read_text())
        d1 = LocalD1(connection)
        asyncio.run(save_results(d1, before))
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

        asyncio.run(save_results(d1, after))

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
        asyncio.run(save_results(d1, after))
        assert connection.execute("SELECT * FROM writes").fetchall() == []

        asyncio.run(save_results(d1, replace(after, num_constituents=0, constituents=[])))
        assert connection.execute("SELECT * FROM constituents").fetchall() == []
