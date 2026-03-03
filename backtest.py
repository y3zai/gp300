"""
G&P 300 Backtest Engine.

Replays historical price data through the index pipeline to validate:
- Divisor continuity at all adjustment events
- Weight sums, caps, entropy ranges
- Constituent count stability
- Expiration removal mechanics

Usage:
    python backtest.py --start 2025-12-01 --end 2026-02-27 [--step 30] [--verbose]

Limitations:
- volume1mo is frozen at current values (no historical volume available)
- Constituent ranking doesn't change; only expiry-driven removals occur
- Closed/resolved markets may have coarser price granularity (12h+)
"""

import argparse
import json
import sys
import time as time_mod
from bisect import bisect_right
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from api import (
    Event, Market, fetch_geopolitics_events, fetch_price_history,
    CLOB_BASE, _safe_float,
)
from engine import (
    IndexConfig, IndexState, Constituent,
    build_constituent_universe, rank_and_select,
    compute_weights, cap_weights, compute_entropy_all, compute_weighted_entropy,
    initialize_index, update_index_value, adjust_divisor,
    remove_dropped_constituents, redistribute_weights,
    normalized_entropy, DEFAULT_CONFIG, _pre_adjustment_value,
)


DATA_DIR = Path("data")


# ──────────────────────────────────────────────
# Price Cache
# ──────────────────────────────────────────────

class PriceCache:
    """
    In-memory cache of historical prices.

    Maps clob_token_id -> sorted list of (unix_ts, price).
    Supports O(log n) lookup via binary search.
    """

    def __init__(self):
        self._data: dict[str, list[tuple[int, float]]] = {}
        self._token_to_market: dict[str, str] = {}  # token_id -> market_id

    def add_history(self, token_id: str, history: list[dict]):
        """Add price history for a token. history = [{"t": ts, "p": price}, ...]"""
        points = sorted([(int(h["t"]), float(h["p"])) for h in history], key=lambda x: x[0])
        self._data[token_id] = points

    def get_price(self, token_id: str, ts: int) -> Optional[float]:
        """
        Get the price at or just before timestamp ts.

        Uses binary search for O(log n) lookup.
        Returns None if no data exists before ts.
        """
        points = self._data.get(token_id)
        if not points:
            return None

        # bisect_right finds insertion point for ts
        idx = bisect_right(points, (ts, float('inf'))) - 1
        if idx < 0:
            return None
        return points[idx][1]

    def get_earliest_ts(self, token_id: str) -> Optional[int]:
        """Get the earliest timestamp available for a token."""
        points = self._data.get(token_id)
        if not points:
            return None
        return points[0][0]

    @property
    def num_tokens(self) -> int:
        return len(self._data)

    @property
    def total_points(self) -> int:
        return sum(len(pts) for pts in self._data.values())

    def save_to_disk(self, path: str):
        """Serialize cache to JSON file."""
        obj = {
            "data": {k: v for k, v in self._data.items()},
            "token_to_market": self._token_to_market,
        }
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)

    @classmethod
    def load_from_disk(cls, path: str) -> "PriceCache":
        """Deserialize cache from JSON file."""
        with open(path, "r") as f:
            obj = json.load(f)
        cache = cls()
        for token_id, points in obj["data"].items():
            cache._data[token_id] = [(int(t), float(p)) for t, p in points]
        cache._token_to_market = obj.get("token_to_market", {})
        return cache


# ──────────────────────────────────────────────
# Token collection & history fetching
# ──────────────────────────────────────────────

def collect_tokens(events: list[Event]) -> dict[str, str]:
    """
    Collect all Yes-token CLOB IDs from events.

    Returns:
        dict mapping clob_token_id -> market_id
    """
    token_map = {}
    for event in events:
        for market in event.markets:
            if market.clob_token_ids and len(market.clob_token_ids) >= 1:
                yes_token = market.clob_token_ids[0]
                token_map[yes_token] = market.id
    return token_map


def fetch_all_histories(
    token_map: dict[str, str],
    start_ts: int,
    end_ts: int,
    fidelity: int = 60,
    batch_delay: float = 0.01,
    verbose: bool = False,
) -> PriceCache:
    """
    Fetch price history for all tokens within a time window.

    Uses interval=max to fetch full history (startTs/endTs rejects long ranges),
    then filters locally to the backtest window.
    Rate limit: 1,000 req/10s — we add small delays to stay safe.
    """
    cache = PriceCache()
    total = len(token_map)
    failed = 0

    for i, (token_id, market_id) in enumerate(token_map.items()):
        history = fetch_price_history(
            clob_token_id=token_id,
            fidelity=fidelity,
            interval="max",
        )
        # Filter to backtest window locally
        if history:
            history = [h for h in history if start_ts <= h["t"] <= end_ts]
        if history:
            cache.add_history(token_id, history)
        else:
            failed += 1
        cache._token_to_market[token_id] = market_id

        if verbose and (i + 1) % 100 == 0:
            print(f"  Fetched {i+1}/{total} ({cache.total_points:,} pts, {failed} empty)")

        if batch_delay > 0:
            time_mod.sleep(batch_delay)

    if verbose or failed > 0:
        print(f"  → {failed} tokens had no data in window")

    return cache


# ──────────────────────────────────────────────
# Price injection
# ──────────────────────────────────────────────

def inject_historical_prices(
    events: list[Event],
    cache: PriceCache,
    ts: int,
) -> list[Event]:
    """
    Create a copy of events with outcome_prices replaced by historical values.

    For each market, looks up the Yes-token price at timestamp ts,
    and sets outcome_prices = [p_yes, 1 - p_yes].

    Markets with no historical data at this time have outcome_prices set to []
    so they are naturally excluded from the universe by downstream guards.
    """
    new_events = []
    for event in events:
        new_markets = []
        any_has_history = False
        for market in event.markets:
            # Look up historical price FIRST to determine active/closed overrides
            hist_price = None
            if market.clob_token_ids and len(market.clob_token_ids) >= 1:
                yes_token = market.clob_token_ids[0]
                hist_price = cache.get_price(yes_token, ts)

            has_history = hist_price is not None
            if has_history:
                any_has_history = True

            m = Market(
                id=market.id,
                question=market.question,
                slug=market.slug,
                condition_id=market.condition_id,
                event_id=market.event_id,
                outcomes=market.outcomes,
                outcome_prices=list(market.outcome_prices),  # copy
                clob_token_ids=market.clob_token_ids,
                volume_total=market.volume_total,
                volume_24h=market.volume_24h,
                volume_1wk=market.volume_1wk,
                volume_1mo=market.volume_1mo,
                volume_1yr=market.volume_1yr,
                liquidity=market.liquidity,
                end_date=market.end_date,
                start_date=market.start_date,
                active=True if has_history else market.active,
                closed=False if has_history else market.closed,
                neg_risk=market.neg_risk,
                group_item_title=market.group_item_title,
                last_trade_price=market.last_trade_price,
                one_day_price_change=market.one_day_price_change,
            )

            # Inject historical price
            if has_history:
                p_yes = max(0.001, min(0.999, hist_price))
                m.outcome_prices = [p_yes, 1.0 - p_yes]
            else:
                m.outcome_prices = []

            new_markets.append(m)

        new_event = Event(
            id=event.id,
            title=event.title,
            slug=event.slug,
            volume_total=event.volume_total,
            volume_24h=event.volume_24h,
            volume_1mo=event.volume_1mo,
            liquidity=event.liquidity,
            end_date=event.end_date,
            start_date=event.start_date,
            active=True if any_has_history else event.active,
            closed=False if any_has_history else event.closed,
            neg_risk=event.neg_risk,
            markets=new_markets,
        )
        new_events.append(new_event)

    return new_events


# ──────────────────────────────────────────────
# Backtest scheduler
# ──────────────────────────────────────────────

def is_weekly_boundary(prev_dt: datetime, curr_dt: datetime) -> bool:
    """Check if we've crossed a Monday 00:00 UTC boundary."""
    prev_monday = prev_dt - timedelta(days=prev_dt.weekday())
    curr_monday = curr_dt - timedelta(days=curr_dt.weekday())
    return curr_monday.date() > prev_monday.date()


def is_biweekly_boundary(start_dt: datetime, curr_dt: datetime) -> bool:
    """Check if we've crossed a biweekly boundary from the start date."""
    days_elapsed = (curr_dt - start_dt).days
    return days_elapsed > 0 and days_elapsed % 14 == 0


# ──────────────────────────────────────────────
# Main backtest loop
# ──────────────────────────────────────────────

def run_backtest(
    events: list[Event],
    cache: PriceCache,
    start_dt: datetime,
    end_dt: datetime,
    step_minutes: int = 30,
    config: IndexConfig = DEFAULT_CONFIG,
    verbose: bool = False,
) -> dict:
    """
    Run the backtest simulation.

    Returns dict with:
        - history: list of {timestamp, value, num_constituents, weighted_entropy}
        - log: list of adjustment events
        - validation: summary of checks
    """
    history = []
    log = []
    state: Optional[IndexState] = None

    step = timedelta(minutes=step_minutes)
    current_dt = start_dt
    prev_dt = start_dt - step  # so first step triggers init
    step_count = 0
    total_steps = int((end_dt - start_dt).total_seconds() / (step_minutes * 60))

    # Validation accumulators
    val = {
        "continuity_violations": [],
        "weight_sum_violations": [],
        "weight_cap_violations": [],
        "entropy_range_violations": [],
        "max_constituents": 0,
        "min_constituents": float('inf'),
        "total_rebalances": 0,
        "total_reconstitutions": 0,
        "total_expirations_removed": 0,
        "total_reconstitution_removed": 0,
    }

    print(f"\nBacktest: {start_dt.date()} → {end_dt.date()}")
    print(f"Steps: {total_steps} × {step_minutes}min")
    print(f"Config: target={config.target_count}, buffer={config.buffer_top}/{config.buffer_bottom}, cap={config.weight_cap}")
    print()

    while current_dt <= end_dt:
        ts = int(current_dt.timestamp())
        is_first = state is None

        # Determine what to do this step
        do_reconstitute = False
        do_rebalance = False

        if is_first:
            do_reconstitute = True
        elif is_biweekly_boundary(start_dt, current_dt) and current_dt.hour == 0 and current_dt.minute == 0:
            do_reconstitute = True
        elif is_weekly_boundary(prev_dt, current_dt) and current_dt.hour == 0 and current_dt.minute == 0:
            do_rebalance = True

        # Inject historical prices
        hist_events = inject_historical_prices(events, cache, ts)

        if is_first or do_reconstitute:
            # Full eligibility filtering for reconstitution
            universe = build_constituent_universe(hist_events, current_dt, config, eligibility_filter=True)
            # Select constituents
            current_ids = None
            if state and not is_first and state.constituents:
                current_ids = {c.id for c in state.constituents}

            prev_ids = current_ids.copy() if current_ids else set()
            selected = rank_and_select(universe, current_ids, config)

            if not selected:
                if is_first:
                    state = initialize_index([], current_dt, config)
                    log.append({
                        "timestamp": current_dt.isoformat(),
                        "event": "initialization",
                        "num_constituents": 0,
                        "index_value": round(state.value, 4),
                        "divisor": state.divisor,
                        "weighted_entropy": 0.0,
                    })
                else:
                    # Preserve index level through empty reconstitution
                    relaxed = build_constituent_universe(hist_events, current_dt, config, eligibility_filter=False)
                    relaxed_map = {c.id: c for c in relaxed}
                    divisor_before = state.divisor
                    pre_val = _pre_adjustment_value(state, relaxed_map)
                    state = IndexState(
                        value=pre_val, divisor=0.0, weighted_entropy=0.0,
                        num_constituents=0, timestamp=current_dt, constituents=[],
                        pre_adjustment_value=pre_val,
                    )
                    log.append({
                        "timestamp": current_dt.isoformat(),
                        "event": "reconstitution",
                        "divisor_before": divisor_before,
                        "divisor_after": 0.0,
                        "index_before": round(pre_val, 4),
                        "index_after": round(pre_val, 4),
                        "num_constituents": 0,
                        "added_count": 0,
                        "removed_count": len(prev_ids),
                    })
                    val["total_reconstitutions"] += 1
                    val["total_reconstitution_removed"] += len(prev_ids)
                history.append({
                    "timestamp": current_dt.isoformat(),
                    "ts": ts,
                    "value": round(state.value, 4),
                    "num_constituents": state.num_constituents,
                    "weighted_entropy": round(state.weighted_entropy, 6),
                    "divisor": state.divisor,
                })
                prev_dt = current_dt
                current_dt += step
                continue

            # Compute weights
            selected = compute_weights(selected, config)

            # Compute entropy
            selected = compute_entropy_all(selected)

            if is_first:
                state = initialize_index(selected, current_dt, config)

                log.append({
                    "timestamp": current_dt.isoformat(),
                    "event": "initialization",
                    "num_constituents": len(selected),
                    "index_value": round(state.value, 4),
                    "divisor": state.divisor,
                    "weighted_entropy": round(state.weighted_entropy, 6),
                })
            else:
                # Build relaxed universe for true pre-adjustment value
                relaxed = build_constituent_universe(hist_events, current_dt, config, eligibility_filter=False)
                relaxed_map = {c.id: c for c in relaxed}
                pre_val = _pre_adjustment_value(state, relaxed_map)

                index_before = pre_val
                divisor_before = state.divisor
                new_divisor = adjust_divisor(state, selected, pre_adj_value=pre_val)
                we = compute_weighted_entropy(selected)
                value = we / new_divisor if new_divisor > 0 else 0.0

                state = IndexState(
                    value=value,
                    divisor=new_divisor,
                    weighted_entropy=we,
                    num_constituents=len(selected),
                    timestamp=current_dt,
                    constituents=selected,
                )

                new_ids = {c.id for c in selected}
                added = new_ids - prev_ids
                removed = prev_ids - new_ids

                log_entry = {
                    "timestamp": current_dt.isoformat(),
                    "event": "reconstitution",
                    "divisor_before": divisor_before,
                    "divisor_after": new_divisor,
                    "index_before": round(index_before, 4),
                    "index_after": round(value, 4),
                    "num_constituents": len(selected),
                    "added_count": len(added),
                    "removed_count": len(removed),
                }
                log.append(log_entry)
                val["total_reconstitutions"] += 1
                val["total_reconstitution_removed"] += len(removed)

                # Check continuity
                if abs(index_before - value) > 0.01:
                    val["continuity_violations"].append({
                        "timestamp": current_dt.isoformat(),
                        "event": "reconstitution",
                        "before": round(index_before, 6),
                        "after": round(value, 6),
                        "diff": round(value - index_before, 6),
                    })

                if verbose:
                    print(f"  [{current_dt.strftime('%Y-%m-%d %H:%M')}] RECONSTITUTION: "
                          f"+{len(added)}/-{len(removed)} → {len(selected)} constituents, "
                          f"index={value:.2f}")

        elif do_rebalance:
            # Relaxed universe (no volume/activity filter)
            universe = build_constituent_universe(hist_events, current_dt, config, eligibility_filter=False)
            universe_map = {c.id: c for c in universe}

            # Detect mid-cycle removals before rebalance
            selected, removed = remove_dropped_constituents(state.constituents, universe_map)

            # Compute true pre-adjustment value at current prices
            pre_val = _pre_adjustment_value(state, universe_map)

            if removed and not selected:
                # All constituents removed during rebalance — preserve value
                divisor_before = state.divisor
                state = IndexState(
                    value=pre_val, divisor=0.0, weighted_entropy=0.0,
                    num_constituents=0, timestamp=current_dt, constituents=[],
                    removed_constituents=removed, pre_adjustment_value=pre_val,
                )
                log.append({
                    "timestamp": current_dt.isoformat(),
                    "event": "expiration_removal",
                    "removed_ids": [c.id for c in removed],
                    "removed_count": len(removed),
                    "divisor_before": divisor_before,
                    "divisor_after": 0.0,
                    "index_before": round(pre_val, 4),
                    "index_after": round(pre_val, 4),
                    "num_constituents": 0,
                })
                val["total_expirations_removed"] += len(removed)
                history.append({
                    "timestamp": current_dt.isoformat(),
                    "ts": ts,
                    "value": round(state.value, 4),
                    "num_constituents": state.num_constituents,
                    "weighted_entropy": round(state.weighted_entropy, 6),
                    "divisor": state.divisor,
                })
                prev_dt = current_dt
                current_dt += step
                continue

            if removed:
                # Log expiration removals
                selected = redistribute_weights(selected)
                selected = cap_weights(selected, config.weight_cap)
                selected = compute_entropy_all(selected)
                removal_divisor = adjust_divisor(state, selected, pre_adj_value=pre_val)
                removal_we = compute_weighted_entropy(selected)
                removal_value = removal_we / removal_divisor if removal_divisor > 0 else 0.0

                log.append({
                    "timestamp": current_dt.isoformat(),
                    "event": "expiration_removal",
                    "removed_ids": [c.id for c in removed],
                    "removed_count": len(removed),
                    "divisor_before": state.divisor,
                    "divisor_after": removal_divisor,
                    "index_before": round(pre_val, 4),
                    "index_after": round(removal_value, 4),
                    "num_constituents": len(selected),
                })
                val["total_expirations_removed"] += len(removed)

                # Check continuity for removal
                if abs(pre_val - removal_value) > 0.01:
                    val["continuity_violations"].append({
                        "timestamp": current_dt.isoformat(),
                        "event": "expiration_removal",
                        "before": round(pre_val, 6),
                        "after": round(removal_value, 6),
                        "diff": round(removal_value - pre_val, 6),
                    })

                # Update state before rebalance
                state = IndexState(
                    value=removal_value,
                    divisor=removal_divisor,
                    weighted_entropy=removal_we,
                    num_constituents=len(selected),
                    timestamp=current_dt,
                    constituents=selected,
                )

                if verbose:
                    print(f"  [{current_dt.strftime('%Y-%m-%d %H:%M')}] EXPIRATION: "
                          f"-{len(removed)} → {len(selected)} constituents, "
                          f"index={removal_value:.2f}")

            # Now do the rebalance (skip if no constituents remain)
            if not selected:
                # Nothing to rebalance — preserve current state
                history.append({
                    "timestamp": current_dt.isoformat(),
                    "ts": ts,
                    "value": round(state.value, 4),
                    "num_constituents": state.num_constituents,
                    "weighted_entropy": round(state.weighted_entropy, 6),
                    "divisor": state.divisor,
                })
                prev_dt = current_dt
                current_dt += step
                continue

            selected = compute_weights(selected, config)
            selected = compute_entropy_all(selected)

            # After removal, state.value is correct; otherwise use pre_val
            rebalance_anchor = None if removed else pre_val
            index_before = state.value if removed else pre_val
            divisor_before = state.divisor
            new_divisor = adjust_divisor(state, selected, pre_adj_value=rebalance_anchor)
            we = compute_weighted_entropy(selected)
            value = we / new_divisor if new_divisor > 0 else 0.0

            state = IndexState(
                value=value,
                divisor=new_divisor,
                weighted_entropy=we,
                num_constituents=len(selected),
                timestamp=current_dt,
                constituents=selected,
            )

            log_entry = {
                "timestamp": current_dt.isoformat(),
                "event": "rebalance",
                "divisor_before": divisor_before,
                "divisor_after": new_divisor,
                "index_before": round(index_before, 4),
                "index_after": round(value, 4),
                "num_constituents": len(selected),
                "weight_max": round(max(c.weight for c in selected), 6),
                "weight_sum": round(sum(c.weight for c in selected), 6),
            }
            log.append(log_entry)
            val["total_rebalances"] += 1

            # Check continuity
            if abs(index_before - value) > 0.01:
                val["continuity_violations"].append({
                    "timestamp": current_dt.isoformat(),
                    "event": "rebalance",
                    "before": round(index_before, 6),
                    "after": round(value, 6),
                    "diff": round(value - index_before, 6),
                })

            if verbose:
                print(f"  [{current_dt.strftime('%Y-%m-%d %H:%M')}] REBALANCE: "
                      f"{len(selected)} constituents, index={value:.2f}")

        else:
            # Regular price update with relaxed universe
            universe = build_constituent_universe(hist_events, current_dt, config, eligibility_filter=False)
            universe_map = {c.id: c for c in universe}

            # Detect mid-cycle removals (expired/closed)
            selected, removed = remove_dropped_constituents(state.constituents, universe_map)

            # Compute true pre-adjustment value at current prices
            pre_val = _pre_adjustment_value(state, universe_map)

            if removed:
                # Redistribute weights and adjust divisor
                selected = redistribute_weights(selected)
                selected = cap_weights(selected, config.weight_cap)

                if not selected:
                    # All removed — preserve value
                    divisor_before = state.divisor
                    state = IndexState(
                        value=pre_val, divisor=0.0, weighted_entropy=0.0,
                        num_constituents=0, timestamp=current_dt, constituents=[],
                        removed_constituents=removed, pre_adjustment_value=pre_val,
                    )
                    log.append({
                        "timestamp": current_dt.isoformat(),
                        "event": "expiration_removal",
                        "removed_ids": [c.id for c in removed],
                        "removed_count": len(removed),
                        "divisor_before": divisor_before,
                        "divisor_after": 0.0,
                        "index_before": round(pre_val, 4),
                        "index_after": round(pre_val, 4),
                        "num_constituents": 0,
                    })
                    val["total_expirations_removed"] += len(removed)
                    history.append({
                        "timestamp": current_dt.isoformat(),
                        "ts": ts,
                        "value": round(state.value, 4),
                        "num_constituents": state.num_constituents,
                        "weighted_entropy": round(state.weighted_entropy, 6),
                        "divisor": state.divisor,
                    })
                    prev_dt = current_dt
                    current_dt += step
                    continue

                selected = compute_entropy_all(selected)
                new_divisor = adjust_divisor(state, selected, pre_adj_value=pre_val)
                we = compute_weighted_entropy(selected)
                value = we / new_divisor if new_divisor > 0 else 0.0

                log.append({
                    "timestamp": current_dt.isoformat(),
                    "event": "expiration_removal",
                    "removed_ids": [c.id for c in removed],
                    "removed_count": len(removed),
                    "divisor_before": state.divisor,
                    "divisor_after": new_divisor,
                    "index_before": round(pre_val, 4),
                    "index_after": round(value, 4),
                    "num_constituents": len(selected),
                })
                val["total_expirations_removed"] += len(removed)

                # Check continuity
                if abs(pre_val - value) > 0.01:
                    val["continuity_violations"].append({
                        "timestamp": current_dt.isoformat(),
                        "event": "expiration_removal",
                        "before": round(pre_val, 6),
                        "after": round(value, 6),
                        "diff": round(value - pre_val, 6),
                    })

                state = IndexState(
                    value=value,
                    divisor=new_divisor,
                    weighted_entropy=we,
                    num_constituents=len(selected),
                    timestamp=current_dt,
                    constituents=selected,
                    removed_constituents=removed,
                )

                if verbose:
                    print(f"  [{current_dt.strftime('%Y-%m-%d %H:%M')}] EXPIRATION: "
                          f"-{len(removed)} → {len(selected)} constituents, "
                          f"index={value:.2f}")
            else:
                if selected:
                    selected = compute_entropy_all(selected)
                    state = update_index_value(state, selected, current_dt)
                # else: 0 constituents, preserve state.value

        # Record history point
        history.append({
            "timestamp": current_dt.isoformat(),
            "ts": ts,
            "value": round(state.value, 4),
            "num_constituents": state.num_constituents,
            "weighted_entropy": round(state.weighted_entropy, 6),
            "divisor": state.divisor,
        })

        # Validation checks
        if state.constituents:
            wsum = sum(c.weight for c in state.constituents)
            wmax = max(c.weight for c in state.constituents)
            e_vals = [c.normalized_entropy for c in state.constituents]

            if abs(wsum - 1.0) > 0.001:
                val["weight_sum_violations"].append({
                    "timestamp": current_dt.isoformat(),
                    "weight_sum": round(wsum, 6),
                })

            if wmax > config.weight_cap + 0.001:
                val["weight_cap_violations"].append({
                    "timestamp": current_dt.isoformat(),
                    "max_weight": round(wmax, 6),
                })

            if any(e < -0.001 or e > 1.001 for e in e_vals):
                val["entropy_range_violations"].append({
                    "timestamp": current_dt.isoformat(),
                    "min": round(min(e_vals), 6),
                    "max": round(max(e_vals), 6),
                })

            val["max_constituents"] = max(val["max_constituents"], state.num_constituents)
            val["min_constituents"] = min(val["min_constituents"], state.num_constituents)

        # Progress
        step_count += 1
        if verbose and step_count % 100 == 0:
            pct = step_count / total_steps * 100
            print(f"  Step {step_count}/{total_steps} ({pct:.0f}%) — "
                  f"{current_dt.strftime('%Y-%m-%d %H:%M')} — "
                  f"Index={state.value:.2f}, N={state.num_constituents}")

        prev_dt = current_dt
        current_dt += step

    return {
        "history": history,
        "log": log,
        "validation": val,
    }


# ──────────────────────────────────────────────
# Output & reporting
# ──────────────────────────────────────────────

def print_report(result: dict):
    """Print backtest validation report."""
    val = result["validation"]
    history = result["history"]
    log = result["log"]

    print(f"\n{'='*60}")
    print(f"  G&P 300 BACKTEST REPORT")
    print(f"{'='*60}")

    if history:
        print(f"\n  Period:       {history[0]['timestamp'][:10]} → {history[-1]['timestamp'][:10]}")
        print(f"  Data points:  {len(history):,}")
        values = [h["value"] for h in history]
        print(f"  Index range:  {min(values):.2f} – {max(values):.2f}")
        print(f"  Start value:  {values[0]:.2f}")
        print(f"  End value:    {values[-1]:.2f}")
        print(f"  Change:       {values[-1] - values[0]:+.2f} ({(values[-1]/values[0]-1)*100:+.1f}%)")

    print(f"\n  Adjustment events:")
    print(f"    Rebalances:       {val['total_rebalances']}")
    print(f"    Reconstitutions:  {val['total_reconstitutions']}")
    print(f"    Expirations out:      {val['total_expirations_removed']}")
    print(f"    Reconstitution out:   {val['total_reconstitution_removed']}")

    print(f"\n  Constituent range:  {val['min_constituents']} – {val['max_constituents']}")

    # Validation results
    print(f"\n  VALIDATION CHECKS:")
    checks = [
        ("Continuity (index_before ≈ index_after)", val["continuity_violations"]),
        ("Weight sum = 1.0", val["weight_sum_violations"]),
        ("Weight cap ≤ 5%", val["weight_cap_violations"]),
        ("Entropy ∈ [0, 1]", val["entropy_range_violations"]),
    ]

    all_pass = True
    for name, violations in checks:
        status = "✅ PASS" if not violations else f"❌ FAIL ({len(violations)} violations)"
        print(f"    {name}: {status}")
        if violations:
            all_pass = False
            for v in violations[:3]:  # show first 3
                print(f"      → {v}")
            if len(violations) > 3:
                print(f"      → ... and {len(violations) - 3} more")

    print(f"\n  Overall: {'✅ ALL CHECKS PASSED' if all_pass else '❌ SOME CHECKS FAILED'}")
    print(f"{'='*60}\n")


def save_results(result: dict):
    """Save backtest results to JSON files."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with open(DATA_DIR / "backtest_history.json", "w") as f:
        json.dump(result["history"], f, indent=2)

    with open(DATA_DIR / "backtest_log.json", "w") as f:
        json.dump(result["log"], f, indent=2)

    with open(DATA_DIR / "backtest_validation.json", "w") as f:
        json.dump(result["validation"], f, indent=2, default=str)

    print(f"Saved: backtest_history.json ({len(result['history']):,} points)")
    print(f"Saved: backtest_log.json ({len(result['log'])} events)")
    print(f"Saved: backtest_validation.json")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="G&P 300 Backtest Engine")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--step", type=int, default=30, help="Step size in minutes (default: 30)")
    parser.add_argument("--fidelity", type=int, default=60, help="Price history fidelity in minutes (default: 60)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--cache", type=str, default=None, help="Path to price cache JSON (load if exists, save after fetch)")

    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if end_dt <= start_dt:
        print("ERROR: end date must be after start date")
        sys.exit(1)

    print("="*60)
    print("  G&P 300 Backtest")
    print("="*60)

    # Step 1: Fetch current events
    print(f"\n[1/3] Fetching geopolitics events from Polymarket...")
    open_events = fetch_geopolitics_events(
        closed=False,
        start_date_max=end_dt,
    )
    closed_events = fetch_geopolitics_events(
        closed=True,
        start_date_max=end_dt,
        end_date_min=start_dt,
    )
    # Deduplicate by event ID (some events may appear in both)
    seen = {}
    for e in open_events + closed_events:
        if e.id not in seen:
            seen[e.id] = e  # open takes priority (listed first)
    events = list(seen.values())
    total_markets = sum(len(e.markets) for e in events)
    print(f"  → {len(events)} events ({len(open_events)} open + {len(closed_events)} closed, deduped), {total_markets} markets")

    # Pre-select: rank all events/markets by volume, ignoring active/closed status.
    # Closed-now markets may have valid historical data needed for backtest.
    ranked_items = []  # (volume_1mo, set_of_market_ids)
    for event in events:
        if event.neg_risk:
            eligible = [m for m in event.markets
                        if m.clob_token_ids and (m.start_date is None or m.start_date <= end_dt)]
            vol = sum(m.volume_1mo for m in eligible)
            mids = {m.id for m in eligible}
            if mids:
                ranked_items.append((vol, mids))
        else:
            for m in event.markets:
                if m.clob_token_ids and (m.start_date is None or m.start_date <= end_dt):
                    ranked_items.append((m.volume_1mo, {m.id}))

    ranked_items.sort(key=lambda x: x[0], reverse=True)
    keep_market_ids = set()
    for vol, mids in ranked_items[:DEFAULT_CONFIG.buffer_bottom + 50]:
        keep_market_ids.update(mids)

    # Step 2: Collect tokens only for relevant markets
    print(f"\n[2/3] Fetching price histories...")
    token_map = {}
    for event in events:
        for market in event.markets:
            if market.id in keep_market_ids and market.clob_token_ids:
                token_map[market.clob_token_ids[0]] = market.id
    print(f"  → {len(token_map)} tokens to fetch (filtered from {total_markets})")

    cache_path = args.cache
    if cache_path and Path(cache_path).exists():
        print(f"  Loading cached prices from {cache_path}...")
        cache = PriceCache.load_from_disk(cache_path)
        print(f"  → {cache.num_tokens} tokens loaded, {cache.total_points:,} price points (from disk)")
    else:
        t0 = time_mod.time()
        # Add 1-day buffer on each side for price lookups at boundaries
        fetch_start = int((start_dt - timedelta(days=1)).timestamp())
        fetch_end = int((end_dt + timedelta(days=1)).timestamp())
        cache = fetch_all_histories(
            token_map,
            start_ts=fetch_start,
            end_ts=fetch_end,
            fidelity=args.fidelity,
            verbose=args.verbose,
        )
        elapsed = time_mod.time() - t0
        print(f"  → {cache.num_tokens} tokens cached, {cache.total_points:,} price points ({elapsed:.1f}s)")
        if cache_path:
            cache.save_to_disk(cache_path)
            print(f"  → Saved cache to {cache_path}")

    # Step 3: Run backtest
    print(f"\n[3/3] Running backtest simulation...")
    t0 = time_mod.time()
    result = run_backtest(
        events=events,
        cache=cache,
        start_dt=start_dt,
        end_dt=end_dt,
        step_minutes=args.step,
        verbose=args.verbose,
    )
    elapsed = time_mod.time() - t0
    print(f"  → {len(result['history']):,} steps simulated ({elapsed:.1f}s)")

    # Report & save
    print_report(result)
    save_results(result)


if __name__ == "__main__":
    main()
