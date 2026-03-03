"""
Targeted unit tests for degenerate engine paths:
- Full removal (regular update & rebalance)
- Empty reconstitution
- Recovery from empty state (target_count vs buffer)
- usable_markets consistency in _build_neg_risk_constituent
"""

import math
from datetime import datetime, timezone, timedelta

from api import Market, Event
from engine import (
    IndexConfig, IndexState, Constituent,
    build_constituent_universe, rank_and_select,
    compute_weights, compute_entropy_all,
    initialize_index, run_full_pipeline,
    normalized_entropy, _build_neg_risk_constituent,
)


# ── Helpers ──────────────────────────────────────


def _make_market(
    mid: str, *, active=True, closed=False, neg_risk=False,
    outcome_prices=None, volume_1mo=5000.0, volume_1wk=100.0,
    end_date=None, start_date=None,
):
    if outcome_prices is None:
        outcome_prices = [0.6, 0.4]
    return Market(
        id=mid, question=f"Q-{mid}", slug=f"slug-{mid}",
        condition_id=f"cond-{mid}", event_id=f"evt-{mid}",
        outcomes=["Yes", "No"], outcome_prices=outcome_prices,
        clob_token_ids=[f"tok-{mid}"],
        volume_total=10000.0, volume_24h=500.0,
        volume_1wk=volume_1wk, volume_1mo=volume_1mo,
        volume_1yr=50000.0, liquidity=1000.0,
        end_date=end_date, start_date=start_date,
        active=active, closed=closed, neg_risk=neg_risk,
        group_item_title=None, last_trade_price=None,
        one_day_price_change=None,
    )


def _make_event(eid, markets, *, neg_risk=False, end_date=None, start_date=None):
    return Event(
        id=eid, title=f"Event-{eid}", slug=f"slug-{eid}",
        volume_total=sum(m.volume_1mo for m in markets),
        volume_24h=0.0, volume_1mo=sum(m.volume_1mo for m in markets),
        liquidity=0.0,
        end_date=end_date, start_date=start_date,
        active=True, closed=False, neg_risk=neg_risk,
        markets=markets,
    )


NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=30)
PAST = NOW - timedelta(days=1)

CFG = IndexConfig(target_count=300, buffer_top=240, buffer_bottom=360)


def _build_initial_state(n=3, config=CFG):
    """Build a valid initial IndexState with n binary constituents."""
    markets = [_make_market(f"m{i}", volume_1mo=10000 - i, end_date=FUTURE)
               for i in range(n)]
    events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(markets)]
    state = run_full_pipeline(events, current_state=None, now=NOW, config=config)
    return state, events


# ── Tests ────────────────────────────────────────


def test_full_removal_regular_update():
    """All constituents expire on a regular update → value preserved, divisor=0."""
    state, _ = _build_initial_state(3)
    assert state.value > 0
    assert state.divisor > 0

    # All markets are now expired (end_date in the past)
    expired_markets = [_make_market(f"m{i}", end_date=PAST, volume_1mo=10000 - i)
                       for i in range(3)]
    expired_events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(expired_markets)]

    new_state = run_full_pipeline(
        expired_events, current_state=state, now=NOW, config=CFG,
        reconstitute=False, rebalance=False,
    )
    assert new_state.divisor == 0.0, f"Expected divisor=0.0, got {new_state.divisor}"
    assert new_state.value > 0, f"Expected value>0, got {new_state.value}"
    assert len(new_state.removed_constituents) == 3
    assert len(new_state.constituents) == 0
    print("  PASS: test_full_removal_regular_update")


def test_full_removal_rebalance():
    """All constituents expire during rebalance → value preserved, divisor=0."""
    state, _ = _build_initial_state(3)
    original_value = state.value

    expired_markets = [_make_market(f"m{i}", end_date=PAST, volume_1mo=10000 - i)
                       for i in range(3)]
    expired_events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(expired_markets)]

    new_state = run_full_pipeline(
        expired_events, current_state=state, now=NOW, config=CFG,
        reconstitute=False, rebalance=True,
    )
    assert new_state.divisor == 0.0, f"Expected divisor=0.0, got {new_state.divisor}"
    assert new_state.value > 0, f"Expected value>0, got {new_state.value}"
    assert len(new_state.removed_constituents) == 3
    assert len(new_state.constituents) == 0
    print("  PASS: test_full_removal_rebalance")


def test_empty_reconstitution():
    """Reconstitute with empty universe → value preserved, divisor=0."""
    state, _ = _build_initial_state(3)
    assert state.value > 0

    # No eligible markets at all
    new_state = run_full_pipeline(
        [], current_state=state, now=NOW, config=CFG,
        reconstitute=True, rebalance=False,
    )
    assert new_state.divisor == 0.0, f"Expected divisor=0.0, got {new_state.divisor}"
    assert new_state.value > 0, f"Expected value>0, got {new_state.value}"
    assert new_state.pre_adjustment_value is not None
    assert len(new_state.constituents) == 0
    print("  PASS: test_empty_reconstitution")


def test_recovery_from_empty_selects_target_count():
    """After full removal (empty state), reconstitution selects target_count, not buffer_top."""
    config = IndexConfig(target_count=10, buffer_top=8, buffer_bottom=12)

    # Build initial state then force empty
    markets = [_make_market(f"m{i}", volume_1mo=10000 - i, end_date=FUTURE)
               for i in range(15)]
    events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(markets)]
    state = run_full_pipeline(events, current_state=None, now=NOW, config=config)
    assert state.num_constituents == 10

    # Force full removal
    expired_markets = [_make_market(f"m{i}", end_date=PAST, volume_1mo=10000 - i)
                       for i in range(15)]
    expired_events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(expired_markets)]
    empty_state = run_full_pipeline(
        expired_events, current_state=state, now=NOW, config=config,
        reconstitute=False, rebalance=False,
    )
    assert empty_state.divisor == 0.0
    assert len(empty_state.constituents) == 0

    # Now reconstitute with 15 eligible markets → should get target_count=10, not buffer_top=8
    fresh_markets = [_make_market(f"new{i}", volume_1mo=10000 - i, end_date=FUTURE)
                     for i in range(15)]
    fresh_events = [_make_event(f"evt-new{i}", [m]) for i, m in enumerate(fresh_markets)]
    recovered_state = run_full_pipeline(
        fresh_events, current_state=empty_state, now=NOW, config=config,
        reconstitute=True, rebalance=False,
    )
    assert recovered_state.num_constituents == config.target_count, (
        f"Expected {config.target_count}, got {recovered_state.num_constituents}"
    )
    print("  PASS: test_recovery_from_empty_selects_target_count")


def test_usable_markets_consistency():
    """negRisk constituent volume and market_ids only reflect usable (priced) markets."""
    # 5 active markets, but 2 lack outcome_prices
    markets = []
    for i in range(5):
        if i < 3:
            prices = [0.2 + i * 0.1]  # valid prices
        else:
            prices = []  # no prices
        markets.append(_make_market(
            f"nr{i}", neg_risk=True,
            outcome_prices=prices,
            volume_1mo=1000.0 * (i + 1),
            volume_1wk=10.0,
            end_date=FUTURE,
        ))

    event = _make_event("nrevt", markets, neg_risk=True, end_date=FUTURE)

    constituent = _build_neg_risk_constituent(event, NOW, CFG, eligibility_filter=True)
    assert constituent is not None, "Expected a constituent to be built"
    # Only 3 markets have valid prices
    assert len(constituent.source_market_ids) == 3, (
        f"Expected 3 source_market_ids, got {len(constituent.source_market_ids)}"
    )
    assert constituent.num_outcomes == 3
    # Volume should only include the 3 usable markets (1000+2000+3000=6000)
    expected_vol = 1000.0 + 2000.0 + 3000.0
    assert constituent.volume_1mo == expected_vol, (
        f"Expected volume {expected_vol}, got {constituent.volume_1mo}"
    )
    # Should NOT include markets nr3, nr4
    assert "nr3" not in constituent.source_market_ids
    assert "nr4" not in constituent.source_market_ids
    print("  PASS: test_usable_markets_consistency")


if __name__ == "__main__":
    print("Running engine edge-case tests...\n")
    test_full_removal_regular_update()
    test_full_removal_rebalance()
    test_empty_reconstitution()
    test_recovery_from_empty_selects_target_count()
    test_usable_markets_consistency()
    print("\nAll tests passed.")
