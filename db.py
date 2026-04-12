"""Data access layer for GP300. Works with both local sqlite3 and Cloudflare D1."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.engine import Constituent, IndexState

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DB_PATH = Path("data/gp300.db")


def connect_local():
    """Connect to local SQLite database, creating schema if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


# ── Write functions ──────────────────────────────


def save_current(conn, state: IndexState):
    """Upsert the current index snapshot."""
    conn.execute(
        "INSERT OR REPLACE INTO current (id, index_value, timestamp, num_constituents, weighted_entropy, divisor) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        (
            round(state.value, 2),
            state.timestamp.isoformat(),
            state.num_constituents,
            round(state.weighted_entropy, 6),
            state.divisor,
        ),
    )
    conn.commit()


def save_constituents(conn, state: IndexState):
    """Replace all constituent rows."""
    conn.execute("DELETE FROM constituents")
    for c in sorted(state.constituents, key=lambda x: x.weight, reverse=True):
        conn.execute(
            "INSERT INTO constituents (id, label, source_type, num_outcomes, probabilities, "
            "normalized_entropy, weight, volume_1mo, end_date, rank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                c.id,
                c.label,
                c.source_type,
                c.num_outcomes,
                json.dumps([round(p, 4) for p in c.probabilities]),
                round(c.normalized_entropy, 4),
                round(c.weight, 6),
                round(c.volume_1mo, 2),
                c.end_date.isoformat() if c.end_date else None,
                c.rank,
            ),
        )
    conn.commit()


def append_history(conn, state: IndexState):
    """Insert a history row (ignore if timestamp already exists)."""
    conn.execute(
        "INSERT OR IGNORE INTO history (timestamp, value, num_constituents, weighted_entropy, divisor) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            state.timestamp.isoformat(),
            round(state.value, 2),
            state.num_constituents,
            round(state.weighted_entropy, 6),
            state.divisor,
        ),
    )
    conn.commit()


def append_adjustment_log(conn, entry: dict):
    """Insert an adjustment log entry."""
    event_type = entry.pop("event")
    timestamp = entry.pop("timestamp")
    conn.execute(
        "INSERT INTO adjustments (timestamp, event, data) VALUES (?, ?, ?)",
        (timestamp, event_type, json.dumps(entry)),
    )
    conn.commit()


def save_state(conn, state: IndexState):
    """Upsert the full engine state for resumption."""
    data = {
        "value": state.value,
        "divisor": state.divisor,
        "weighted_entropy": state.weighted_entropy,
        "num_constituents": state.num_constituents,
        "timestamp": state.timestamp.isoformat(),
        "last_rebase": state.last_rebase.isoformat() if state.last_rebase else None,
        "constituents": [
            {
                "id": c.id,
                "label": c.label,
                "source_type": c.source_type,
                "num_outcomes": c.num_outcomes,
                "probabilities": c.probabilities,
                "volume_1mo": c.volume_1mo,
                "end_date": c.end_date.isoformat() if c.end_date else None,
                "weight": c.weight,
                "rank": c.rank,
                "normalized_entropy": c.normalized_entropy,
                "source_market_ids": c.source_market_ids,
            }
            for c in state.constituents
        ],
    }
    conn.execute(
        "INSERT OR REPLACE INTO state (id, data) VALUES (1, ?)",
        (json.dumps(data),),
    )
    conn.commit()


def load_state(conn) -> IndexState | None:
    """Load previous engine state from the state table."""
    row = conn.execute("SELECT data FROM state WHERE id = 1").fetchone()
    if row is None:
        return None

    data = json.loads(row["data"])

    constituents = []
    for cd in data.get("constituents", []):
        end_date = None
        if cd.get("end_date"):
            try:
                end_date = datetime.fromisoformat(cd["end_date"])
            except (ValueError, TypeError):
                pass

        c = Constituent(
            id=cd["id"],
            label=cd["label"],
            source_type=cd["source_type"],
            num_outcomes=cd["num_outcomes"],
            probabilities=cd["probabilities"],
            volume_1mo=cd["volume_1mo"],
            end_date=end_date,
            weight=cd.get("weight", 0.0),
            rank=cd.get("rank", 0),
            normalized_entropy=cd.get("normalized_entropy", 0.0),
            source_market_ids=cd.get("source_market_ids", []),
        )
        constituents.append(c)

    ts = datetime.fromisoformat(data["timestamp"])
    last_rebase = None
    if data.get("last_rebase"):
        try:
            last_rebase = datetime.fromisoformat(data["last_rebase"])
        except (ValueError, TypeError):
            pass

    return IndexState(
        value=data["value"],
        divisor=data["divisor"],
        weighted_entropy=data["weighted_entropy"],
        num_constituents=data["num_constituents"],
        timestamp=ts,
        constituents=constituents,
        last_rebase=last_rebase,
    )


# ── Query functions (for Worker API endpoints) ──


def get_current(conn) -> dict | None:
    """Return the current index snapshot as a dict."""
    row = conn.execute("SELECT * FROM current WHERE id = 1").fetchone()
    if row is None:
        return None
    return {
        "index_value": row["index_value"],
        "timestamp": row["timestamp"],
        "num_constituents": row["num_constituents"],
        "weighted_entropy": row["weighted_entropy"],
        "divisor": row["divisor"],
    }


def get_constituents(conn) -> list[dict]:
    """Return all constituents ordered by weight descending."""
    rows = conn.execute(
        "SELECT * FROM constituents ORDER BY weight DESC"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "label": r["label"],
            "source_type": r["source_type"],
            "num_outcomes": r["num_outcomes"],
            "probabilities": json.loads(r["probabilities"]),
            "normalized_entropy": r["normalized_entropy"],
            "weight": r["weight"],
            "volume_1mo": r["volume_1mo"],
            "end_date": r["end_date"],
            "rank": r["rank"],
        }
        for r in rows
    ]


def get_history(conn, since: str | None = None) -> list[dict]:
    """Return history rows, optionally filtered by timestamp >= since."""
    if since:
        rows = conn.execute(
            "SELECT * FROM history WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM history ORDER BY timestamp"
        ).fetchall()
    return [
        {
            "timestamp": r["timestamp"],
            "value": r["value"],
            "num_constituents": r["num_constituents"],
            "weighted_entropy": r["weighted_entropy"],
            "divisor": r["divisor"],
        }
        for r in rows
    ]
