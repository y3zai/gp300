import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from src import update_index
from src.d1_state import select_update


def slot(hour, minute, day=4):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def test_rest_runner_leaves_duplicate_and_racing_updates_unpublished(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(Path(__file__).with_name("schema.sql").read_text())
    batch_changes = []

    def d1(request):
        assert request.headers["Authorization"] == "Bearer test-token"
        body = json.loads(request.content)
        batch = body.get("batch")
        queries = batch or [body]
        results = []
        with conn:
            for query in queries:
                params = query.get("params", [])
                assert all(isinstance(value, str) for value in params)
                cursor = conn.execute(query["sql"], params)
                results.append({
                    "success": True,
                    "results": [dict(row) for row in cursor.fetchall()],
                    "meta": {"changes": max(cursor.rowcount, 0)},
                })
        if batch:
            batch_changes.append(results[-1]["meta"]["changes"])
        return httpx.Response(200, json={"success": True, "result": results})

    with httpx.Client(transport=httpx.MockTransport(d1)) as client:
        db = update_index.D1Client(client, "account", "database", "test-token")
        monkeypatch.setattr(update_index, "fetch_events", lambda: [])
        first = slot(15, 20)
        assert update_index.run_update(db, first, now=first + timedelta(seconds=10))
        assert not update_index.run_update(db, first, now=first + timedelta(minutes=1))

        def fetch_with_race():
            monkeypatch.setattr(update_index, "fetch_events", lambda: [])
            later = slot(15, 30)
            assert update_index.run_update(db, later, now=later + timedelta(seconds=10))
            return []

        monkeypatch.setattr(update_index, "fetch_events", fetch_with_race)
        older = slot(15, 25)
        assert not update_index.run_update(db, older, now=older + timedelta(seconds=10))
        assert batch_changes == [1, 1, 0]
        assert conn.execute("SELECT timestamp FROM current").fetchone()[0] == slot(
            15, 30
        ).replace(second=10).isoformat()
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0] == 1


def test_regular_slot_can_recover_missed_sunday_maintenance():
    # November 1, 2026 was Sunday.
    mode = select_update(None, slot(0, 0, day=1))
    assert mode.reconstitute and not mode.rebalance
    mode = select_update(None, slot(0, 5, day=1))
    assert mode.reconstitute and mode.rebalance
    assert mode.maintenance_date == "2026-11-01"
