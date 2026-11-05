"""Serialization and transactional publication for the index runner."""

import json
from dataclasses import dataclass
from datetime import datetime, time

from .engine import Constituent, IndexState


STATE_QUERY = "SELECT data FROM state WHERE id = 1"
STATE_MATCH = "COALESCE((SELECT json_extract(data, '$.timestamp') FROM state WHERE id = 1), '') = ?"


@dataclass
class PublishedState:
    state: IndexState
    version: str
    scheduled_at: datetime | None
    maintenance_date: str | None


@dataclass
class UpdateMode:
    reconstitute: bool
    rebalance: bool
    maintenance_date: str | None


def select_update(previous, scheduled_at):
    """Skip completed slots and catch up a daily adjustment delayed past 00:01 UTC."""
    maintenance_date = previous.maintenance_date if previous else None
    if previous:
        previous_slot = previous.scheduled_at or previous.state.timestamp
        if scheduled_at <= previous_slot:
            return None
        if previous.scheduled_at is None and previous_slot.time() >= time(0, 1):
            # A pre-migration state came from the existing midnight cron schedule.
            maintenance_date = previous_slot.date().isoformat()

    today = scheduled_at.date().isoformat()
    maintenance = scheduled_at.time() >= time(0, 1) and maintenance_date != today
    return UpdateMode(
        reconstitute=previous is None or (maintenance and scheduled_at.weekday() == 6),
        rebalance=maintenance,
        maintenance_date=today if maintenance else maintenance_date,
    )


def decode_state(raw):
    """Read both legacy and slot-aware engine snapshots."""
    if raw is None:
        return None
    data = json.loads(raw)
    constituents = []
    for cd in data.get("constituents", []):
        end_date = None
        if cd.get("end_date"):
            try:
                end_date = datetime.fromisoformat(cd["end_date"])
            except (ValueError, TypeError):
                pass
        constituents.append(
            Constituent(
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
        )

    ts = datetime.fromisoformat(data["timestamp"])
    last_rebase = None
    if data.get("last_rebase"):
        try:
            last_rebase = datetime.fromisoformat(data["last_rebase"])
        except (ValueError, TypeError):
            pass
    state = IndexState(
        value=data["value"],
        divisor=data["divisor"],
        weighted_entropy=data["weighted_entropy"],
        num_constituents=data["num_constituents"],
        timestamp=ts,
        constituents=constituents,
        last_rebase=last_rebase,
    )
    slot = data.get("scheduled_at")
    return PublishedState(
        state=state,
        version=data["timestamp"],
        scheduled_at=datetime.fromisoformat(slot) if slot else None,
        maintenance_date=data.get("maintenance_date"),
    )


def _make_constituent_snapshot(constituents):
    """Return a compact list of constituent dicts for adjustment logs."""
    return [
        {
            "id": c.id,
            "label": c.label,
            "source_type": c.source_type,
            "weight": round(c.weight, 6),
            "rank": c.rank,
            "normalized_entropy": round(c.normalized_entropy, 4),
            "volume_1mo": round(c.volume_1mo, 2),
        }
        for c in sorted(constituents, key=lambda x: x.weight, reverse=True)
    ]


def build_d1_update(state, previous_publication, scheduled_at, mode):
    """Build one atomic update guarded by the version read before computing."""
    previous = previous_publication.state if previous_publication else None
    if previous is not None and state.timestamp <= previous.timestamp:
        raise ValueError("observation time must advance the published state")
    expected_version = previous_publication.version if previous_publication else ""
    is_first_run = previous is None
    reconstitute = mode.reconstitute
    rebalance = mode.rebalance
    stmts = []

    # current
    stmts.append((
        "INSERT OR REPLACE INTO current "
        "(id, index_value, timestamp, num_constituents, weighted_entropy, divisor) "
        f"SELECT 1, ?, ?, ?, ?, ? WHERE {STATE_MATCH}",
        (
            round(state.value, 2),
            state.timestamp.isoformat(),
            state.num_constituents,
            round(state.weighted_entropy, 6),
            state.divisor,
            expected_version,
        ),
    ))

    # Reconcile the snapshot in bulk. Updates leave the indexed ID untouched,
    # and null-safe comparison skips rows whose published values are unchanged.
    constituents = json.dumps([
        {
            "id": c.id,
            "label": c.label,
            "source_type": c.source_type,
            "num_outcomes": c.num_outcomes,
            "probabilities": json.dumps([round(p, 4) for p in c.probabilities]),
            "normalized_entropy": round(c.normalized_entropy, 4),
            "weight": round(c.weight, 6),
            "volume_1mo": round(c.volume_1mo, 2),
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "rank": c.rank,
        }
        for c in sorted(state.constituents, key=lambda x: x.weight, reverse=True)
    ])
    stmts.append((
        "DELETE FROM constituents "
        f"WHERE {STATE_MATCH} "
        "AND (id IS NULL OR id NOT IN (SELECT value ->> 'id' FROM json_each(?)))",
        (expected_version, constituents),
    ))
    stmts.append((f"""
        INSERT INTO constituents (
            id, label, source_type, num_outcomes, probabilities,
            normalized_entropy, weight, volume_1mo, end_date, rank
        )
        SELECT
            value ->> 'id', value ->> 'label', value ->> 'source_type',
            value ->> 'num_outcomes', value ->> 'probabilities',
            value ->> 'normalized_entropy', value ->> 'weight',
            value ->> 'volume_1mo', value ->> 'end_date', value ->> 'rank'
        FROM json_each(?) WHERE {STATE_MATCH}
        ON CONFLICT(id) DO UPDATE SET
            label = excluded.label,
            source_type = excluded.source_type,
            num_outcomes = excluded.num_outcomes,
            probabilities = excluded.probabilities,
            normalized_entropy = excluded.normalized_entropy,
            weight = excluded.weight,
            volume_1mo = excluded.volume_1mo,
            end_date = excluded.end_date,
            rank = excluded.rank
        WHERE (
            constituents.label, constituents.source_type, constituents.num_outcomes,
            constituents.probabilities, constituents.normalized_entropy, constituents.weight,
            constituents.volume_1mo, constituents.end_date, constituents.rank
        ) IS NOT (
            excluded.label, excluded.source_type, excluded.num_outcomes,
            excluded.probabilities, excluded.normalized_entropy, excluded.weight,
            excluded.volume_1mo, excluded.end_date, excluded.rank
        )
    """, (constituents, expected_version)))

    # history (append)
    stmts.append((
        "INSERT OR IGNORE INTO history "
        "(timestamp, value, num_constituents, weighted_entropy, divisor) "
        f"SELECT ?, ?, ?, ?, ? WHERE {STATE_MATCH}",
        (
            state.timestamp.isoformat(),
            round(state.value, 2),
            state.num_constituents,
            round(state.weighted_entropy, 6),
            state.divisor,
            expected_version,
        ),
    ))

    # state (full engine blob for resumption)
    state_data = {
        "value": state.value,
        "divisor": state.divisor,
        "weighted_entropy": state.weighted_entropy,
        "num_constituents": state.num_constituents,
        "timestamp": state.timestamp.isoformat(),
        "scheduled_at": scheduled_at.isoformat(),
        "maintenance_date": mode.maintenance_date,
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

    # adjustments (audit trail)
    adjustment = None

    if is_first_run:
        adj_data = {
            "num_constituents": state.num_constituents,
            "index_value": round(state.value, 2),
            "divisor": state.divisor,
            "weighted_entropy": round(state.weighted_entropy, 6),
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        adjustment = ("initialization", adj_data)

    elif state.rebased and previous is not None:
        # Bimonthly rebase — index reset to base value
        adj_data = {
            "divisor_before": previous.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(previous.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        adjustment = ("rebase", adj_data)

    elif reconstitute and previous is not None:
        prev_ids = {c.id for c in previous.constituents}
        curr_ids = {c.id for c in state.constituents}
        added_ids = sorted(curr_ids - prev_ids)
        removed_ids = sorted(prev_ids - curr_ids)
        adj_data = {
            "divisor_before": previous.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(previous.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "added_ids": added_ids,
            "removed_ids": removed_ids,
            "added_count": len(added_ids),
            "removed_count": len(removed_ids),
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        adjustment = ("reconstitution", adj_data)

    elif rebalance:
        weights = [c.weight for c in state.constituents]
        adj_data = {
            "divisor_before": previous.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(previous.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "weight_max": round(max(weights, default=0.0), 6),
            "weight_sum": round(sum(weights), 6),
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        adjustment = ("rebalance", adj_data)

    elif state.removed_constituents:
        removed_ids = sorted(c.id for c in state.removed_constituents)
        adj_data = {
            "removed_ids": removed_ids,
            "removed_count": len(removed_ids),
            "divisor_before": previous.divisor,
            "divisor_after": state.divisor,
            "index_before": round(state.pre_adjustment_value, 2)
            if state.pre_adjustment_value is not None
            else round(previous.value, 2),
            "index_after": round(state.value, 2),
            "num_constituents": state.num_constituents,
            "constituents": _make_constituent_snapshot(state.constituents),
        }
        adjustment = ("expiration_removal", adj_data)

    if adjustment is not None:
        event, data = adjustment
        stmts.append((
            "INSERT INTO adjustments (timestamp, event, data) "
            f"SELECT ?, ?, ? WHERE {STATE_MATCH}",
            (state.timestamp.isoformat(), event, json.dumps(data), expected_version),
        ))

    # Both writers check the old version on every statement in a transaction.
    # Update the version last, so every prior write is conditional on the same state.
    stmts.append((
        f"INSERT OR REPLACE INTO state (id, data) SELECT 1, ? WHERE {STATE_MATCH}",
        (json.dumps(state_data), expected_version),
    ))
    return stmts
