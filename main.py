"""
G&P 300 Main Entry Point.

Usage:
    python main.py                    # First run: initialize index
    python main.py --update           # Regular 30-min update (prices only)
    python main.py --rebalance        # Weekly: recompute weights
    python main.py --reconstitute     # Biweekly: re-select constituents + weights
    python main.py --dry-run          # Compute but don't write files
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from api import fetch_geopolitics_events
from engine import (
    Constituent,
    IndexState,
    run_full_pipeline,
)

DATA_DIR = Path("data")


# ──────────────────────────────────────────────
# State persistence (JSON files)
# ──────────────────────────────────────────────


def save_current(state: IndexState):
    """Write data/current.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "index_value": round(state.value, 2),
        "timestamp": state.timestamp.isoformat(),
        "num_constituents": state.num_constituents,
        "weighted_entropy": round(state.weighted_entropy, 6),
        "divisor": state.divisor,
    }
    with open(DATA_DIR / "current.json", "w") as f:
        json.dump(data, f, indent=2)


def save_constituents(state: IndexState):
    """Write data/constituents.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = []
    for c in sorted(state.constituents, key=lambda x: x.weight, reverse=True):
        data.append(
            {
                "id": c.id,
                "label": c.label,
                "source_type": c.source_type,
                "num_outcomes": c.num_outcomes,
                "probabilities": [round(p, 4) for p in c.probabilities],
                "normalized_entropy": round(c.normalized_entropy, 4),
                "weight": round(c.weight, 6),
                "volume_1mo": round(c.volume_1mo, 2),
                "end_date": c.end_date.isoformat() if c.end_date else None,
                "rank": c.rank,
            }
        )
    with open(DATA_DIR / "constituents.json", "w") as f:
        json.dump(data, f, indent=2)


def append_history(state: IndexState):
    """Append to data/history.jsonl (JSON Lines format, append-only)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    history_path = DATA_DIR / "history.jsonl"

    entry = {
        "timestamp": state.timestamp.isoformat(),
        "value": round(state.value, 2),
        "num_constituents": state.num_constituents,
        "weighted_entropy": round(state.weighted_entropy, 6),
        "divisor": state.divisor,
    }

    with open(history_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def make_constituent_snapshot(constituents) -> list[dict]:
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


def append_adjustment_log(entry: dict):
    """Append one JSON line to data/adjustments.jsonl."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "adjustments.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def save_state(state: IndexState):
    """
    Save full engine state for resumption.

    This is an internal file (not for the frontend) that stores
    enough info to resume without re-initializing the index.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "value": state.value,
        "divisor": state.divisor,
        "weighted_entropy": state.weighted_entropy,
        "num_constituents": state.num_constituents,
        "timestamp": state.timestamp.isoformat(),
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
    with open(DATA_DIR / "state.json", "w") as f:
        json.dump(data, f, indent=2)


def load_state() -> IndexState | None:
    """Load previous engine state from data/state.json."""
    state_path = DATA_DIR / "state.json"
    if not state_path.exists():
        return None

    with open(state_path) as f:
        data = json.load(f)

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

    return IndexState(
        value=data["value"],
        divisor=data["divisor"],
        weighted_entropy=data["weighted_entropy"],
        num_constituents=data["num_constituents"],
        timestamp=ts,
        constituents=constituents,
    )


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="G&P 300 Index Engine")
    parser.add_argument(
        "--update", action="store_true", help="Regular price update (30-min cycle)"
    )
    parser.add_argument(
        "--rebalance", action="store_true", help="Recompute weights (weekly cycle)"
    )
    parser.add_argument(
        "--reconstitute",
        action="store_true",
        help="Re-select constituents (biweekly cycle)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Compute but don't write files"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print detailed output"
    )

    args = parser.parse_args()
    now = datetime.now(timezone.utc)

    # Determine mode
    is_update = args.update
    is_rebalance = args.rebalance
    is_reconstitute = args.reconstitute

    # If no flag, treat as initialization or reconstitute
    if not any([is_update, is_rebalance, is_reconstitute]):
        is_reconstitute = True

    # Load previous state
    prev_state = load_state()
    is_first_run = prev_state is None

    if (is_update or is_rebalance) and is_first_run:
        print(
            "ERROR: No previous state found. Run without --update first to initialize."
        )
        sys.exit(1)

    # Fetch data
    print(
        f"[{now.strftime('%H:%M:%S')}] Fetching geopolitics events from Polymarket..."
    )
    events = fetch_geopolitics_events(active=True)
    total_markets = sum(len(e.markets) for e in events)
    print(f"  → {len(events)} events, {total_markets} markets")

    # Run pipeline
    mode_str = (
        "INIT"
        if is_first_run
        else (
            "RECONSTITUTE"
            if is_reconstitute
            else "REBALANCE"
            if is_rebalance
            else "UPDATE"
        )
    )
    print(f"[{now.strftime('%H:%M:%S')}] Running pipeline (mode: {mode_str})...")

    state = run_full_pipeline(
        events=events,
        current_state=prev_state,
        now=now,
        reconstitute=is_reconstitute,
        rebalance=is_rebalance,
    )

    # Print summary
    print(f"\n{'=' * 50}")
    print("  G&P 300 Index")
    print(f"{'=' * 50}")
    print(f"  Value:          {state.value:,.2f}")
    print(f"  Constituents:   {state.num_constituents}")
    print(f"  Wtd Entropy:    {state.weighted_entropy:.6f}")
    print(f"  Divisor:        {state.divisor:.10f}")
    print(f"  Timestamp:      {state.timestamp.isoformat()}")
    print(f"{'=' * 50}")

    if args.verbose and state.constituents:
        print("\nTop 20 constituents by weight:")
        print(
            f"{'Rank':>5} {'Weight':>8} {'Entropy':>8} {'Vol30d':>12} {'Type':>6} Label"
        )
        print(f"{'-' * 5} {'-' * 8} {'-' * 8} {'-' * 12} {'-' * 6} {'-' * 40}")
        top = sorted(state.constituents, key=lambda c: c.weight, reverse=True)[:20]
        for c in top:
            print(
                f"{c.rank:>5} {c.weight:>8.4f} {c.normalized_entropy:>8.4f} "
                f"{c.volume_1mo:>12,.0f} {'event' if c.source_type == 'event' else 'mkt':>6} "
                f"{c.label[:50]}"
            )

        # Weight distribution stats
        weights = [c.weight for c in state.constituents]
        print(
            f"\nWeight stats: sum={sum(weights):.6f}, "
            f"max={max(weights):.4f}, min={min(weights):.6f}, "
            f"mean={sum(weights) / len(weights):.6f}"
        )

        # Entropy distribution
        entropies = [c.normalized_entropy for c in state.constituents]
        print(
            f"Entropy stats: mean={sum(entropies) / len(entropies):.4f}, "
            f"max={max(entropies):.4f}, min={min(entropies):.4f}"
        )

        # Source type breakdown
        n_events = sum(1 for c in state.constituents if c.source_type == "event")
        n_markets = sum(1 for c in state.constituents if c.source_type == "market")
        print(f"Types: {n_events} multi-outcome events, {n_markets} binary markets")

    # Write output files
    if not args.dry_run:
        print(f"\nWriting output files to {DATA_DIR}/...")
        save_current(state)
        save_constituents(state)
        append_history(state)
        save_state(state)
        print("  → current.json, constituents.json, history.jsonl, state.json")

        # Append adjustment log for non-trivial events
        ts = state.timestamp.isoformat()

        if is_first_run:
            append_adjustment_log(
                {
                    "event": "initialization",
                    "timestamp": ts,
                    "num_constituents": state.num_constituents,
                    "index_value": round(state.value, 2),
                    "divisor": state.divisor,
                    "weighted_entropy": round(state.weighted_entropy, 6),
                    "constituents": make_constituent_snapshot(state.constituents),
                }
            )
            print("  → adjustments.jsonl (initialization)")

        elif is_reconstitute and prev_state is not None:
            prev_ids = {c.id for c in prev_state.constituents}
            curr_ids = {c.id for c in state.constituents}
            added_ids = sorted(curr_ids - prev_ids)
            removed_ids = sorted(prev_ids - curr_ids)
            append_adjustment_log(
                {
                    "event": "reconstitution",
                    "timestamp": ts,
                    "divisor_before": prev_state.divisor,
                    "divisor_after": state.divisor,
                    "index_before": round(state.pre_adjustment_value, 2)
                    if state.pre_adjustment_value is not None
                    else round(prev_state.value, 2),
                    "index_after": round(state.value, 2),
                    "num_constituents": state.num_constituents,
                    "added_ids": added_ids,
                    "removed_ids": removed_ids,
                    "added_count": len(added_ids),
                    "removed_count": len(removed_ids),
                    "constituents": make_constituent_snapshot(state.constituents),
                }
            )
            print("  → adjustments.jsonl (reconstitution)")

        elif is_rebalance:
            weights = [c.weight for c in state.constituents]
            append_adjustment_log(
                {
                    "event": "rebalance",
                    "timestamp": ts,
                    "divisor_before": prev_state.divisor,
                    "divisor_after": state.divisor,
                    "index_before": round(state.pre_adjustment_value, 2)
                    if state.pre_adjustment_value is not None
                    else round(prev_state.value, 2),
                    "index_after": round(state.value, 2),
                    "num_constituents": state.num_constituents,
                    "weight_max": round(max(weights), 6),
                    "weight_sum": round(sum(weights), 6),
                    "constituents": make_constituent_snapshot(state.constituents),
                }
            )
            print("  → adjustments.jsonl (rebalance)")

        elif is_update and state.removed_constituents:
            removed_ids = sorted(c.id for c in state.removed_constituents)
            append_adjustment_log(
                {
                    "event": "expiration_removal",
                    "timestamp": ts,
                    "removed_ids": removed_ids,
                    "removed_count": len(removed_ids),
                    "divisor_before": prev_state.divisor,
                    "divisor_after": state.divisor,
                    "index_before": round(state.pre_adjustment_value, 2)
                    if state.pre_adjustment_value is not None
                    else round(prev_state.value, 2),
                    "index_after": round(state.value, 2),
                    "num_constituents": state.num_constituents,
                    "constituents": make_constituent_snapshot(state.constituents),
                }
            )
            print("  → adjustments.jsonl (expiration_removal)")

    else:
        print("\n(dry-run: no files written)")


if __name__ == "__main__":
    main()
