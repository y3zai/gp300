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
import sys
from datetime import datetime, timezone

import db
from api import fetch_geopolitics_events
from engine import IndexState, run_full_pipeline


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
    conn = db.connect_local()
    prev_state = db.load_state(conn)
    is_first_run = prev_state is None

    if (is_update or is_rebalance) and is_first_run:
        print("WARNING: No previous state found. Falling back to initialization.")
        is_update = False
        is_rebalance = False
        is_reconstitute = True

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

    # Write to database
    if not args.dry_run:
        print(f"\nWriting to SQLite ({db.DB_PATH})...")
        db.save_current(conn, state)
        db.save_constituents(conn, state)
        db.append_history(conn, state)
        db.save_state(conn, state)
        print("  → current, constituents, history, state")

        # Append adjustment log for non-trivial events
        ts = state.timestamp.isoformat()

        if is_first_run:
            db.append_adjustment_log(
                conn,
                {
                    "event": "initialization",
                    "timestamp": ts,
                    "num_constituents": state.num_constituents,
                    "index_value": round(state.value, 2),
                    "divisor": state.divisor,
                    "weighted_entropy": round(state.weighted_entropy, 6),
                    "constituents": make_constituent_snapshot(state.constituents),
                },
            )
            print("  → adjustments (initialization)")

        elif is_reconstitute and prev_state is not None:
            prev_ids = {c.id for c in prev_state.constituents}
            curr_ids = {c.id for c in state.constituents}
            added_ids = sorted(curr_ids - prev_ids)
            removed_ids = sorted(prev_ids - curr_ids)
            db.append_adjustment_log(
                conn,
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
                },
            )
            print("  → adjustments (reconstitution)")

        elif is_rebalance:
            weights = [c.weight for c in state.constituents]
            db.append_adjustment_log(
                conn,
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
                },
            )
            print("  → adjustments (rebalance)")

        elif is_update and state.removed_constituents:
            removed_ids = sorted(c.id for c in state.removed_constituents)
            db.append_adjustment_log(
                conn,
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
                },
            )
            print("  → adjustments (expiration_removal)")

        conn.close()
    else:
        conn.close()
        print("\n(dry-run: no data written)")


if __name__ == "__main__":
    main()
