from datetime import datetime, timezone

import db
from src.engine import IndexState


def test_save_and_load_state_preserves_last_rebase(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gp300.db")

    conn = db.connect_local()
    ts = datetime(2026, 4, 12, 18, 30, tzinfo=timezone.utc)
    last_rebase = datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc)
    state = IndexState(
        value=1000.0,
        divisor=0.0005,
        weighted_entropy=0.5,
        num_constituents=0,
        timestamp=ts,
        constituents=[],
        last_rebase=last_rebase,
    )

    db.save_state(conn, state)
    loaded = db.load_state(conn)

    assert loaded is not None
    assert loaded.last_rebase == last_rebase
