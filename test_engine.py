"""
Targeted unit tests for degenerate engine paths:
- Full removal (regular update & rebalance)
- Empty reconstitution
- Recovery from empty state (target_count vs buffer)
- usable_markets consistency in _build_neg_risk_constituent
"""

from datetime import datetime, timedelta, timezone

from src.api import Event, Market
from src.engine import (
    IndexConfig,
    _build_neg_risk_constituent,
    run_full_pipeline,
)

# ── Helpers ──────────────────────────────────────


def _make_market(
    mid: str,
    *,
    active=True,
    closed=False,
    neg_risk=False,
    outcome_prices=None,
    volume_1mo=5000.0,
    volume_1wk=100.0,
    end_date=None,
    start_date=None,
):
    if outcome_prices is None:
        outcome_prices = [0.6, 0.4]
    return Market(
        id=mid,
        question=f"Q-{mid}",
        slug=f"slug-{mid}",
        condition_id=f"cond-{mid}",
        event_id=f"evt-{mid}",
        outcomes=["Yes", "No"],
        outcome_prices=outcome_prices,
        clob_token_ids=[f"tok-{mid}"],
        volume_total=10000.0,
        volume_24h=500.0,
        volume_1wk=volume_1wk,
        volume_1mo=volume_1mo,
        volume_1yr=50000.0,
        liquidity=1000.0,
        end_date=end_date,
        start_date=start_date,
        active=active,
        closed=closed,
        neg_risk=neg_risk,
        group_item_title=None,
        last_trade_price=None,
        one_day_price_change=None,
    )


def _make_event(eid, markets, *, neg_risk=False, end_date=None, start_date=None):
    return Event(
        id=eid,
        title=f"Event-{eid}",
        slug=f"slug-{eid}",
        volume_total=sum(m.volume_1mo for m in markets),
        volume_24h=0.0,
        volume_1mo=sum(m.volume_1mo for m in markets),
        liquidity=0.0,
        end_date=end_date,
        start_date=start_date,
        active=True,
        closed=False,
        neg_risk=neg_risk,
        markets=markets,
    )


NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(days=30)
PAST = NOW - timedelta(days=1)

CFG = IndexConfig(target_count=300, buffer_top=240, buffer_bottom=360)


def _build_initial_state(n=3, config=CFG):
    """Build a valid initial IndexState with n binary constituents."""
    markets = [
        _make_market(f"m{i}", volume_1mo=10000 - i, end_date=FUTURE) for i in range(n)
    ]
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
    expired_markets = [
        _make_market(f"m{i}", end_date=PAST, volume_1mo=10000 - i) for i in range(3)
    ]
    expired_events = [
        _make_event(f"evt-m{i}", [m]) for i, m in enumerate(expired_markets)
    ]

    new_state = run_full_pipeline(
        expired_events,
        current_state=state,
        now=NOW,
        config=CFG,
        reconstitute=False,
        rebalance=False,
    )
    assert new_state.divisor == 0.0, f"Expected divisor=0.0, got {new_state.divisor}"
    assert new_state.value > 0, f"Expected value>0, got {new_state.value}"
    assert len(new_state.removed_constituents) == 3
    assert len(new_state.constituents) == 0
    print("  PASS: test_full_removal_regular_update")


def test_full_removal_rebalance():
    """All constituents expire during rebalance → value preserved, divisor=0."""
    state, _ = _build_initial_state(3)

    expired_markets = [
        _make_market(f"m{i}", end_date=PAST, volume_1mo=10000 - i) for i in range(3)
    ]
    expired_events = [
        _make_event(f"evt-m{i}", [m]) for i, m in enumerate(expired_markets)
    ]

    new_state = run_full_pipeline(
        expired_events,
        current_state=state,
        now=NOW,
        config=CFG,
        reconstitute=False,
        rebalance=True,
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
        [],
        current_state=state,
        now=NOW,
        config=CFG,
        reconstitute=True,
        rebalance=False,
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
    markets = [
        _make_market(f"m{i}", volume_1mo=10000 - i, end_date=FUTURE) for i in range(15)
    ]
    events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(markets)]
    state = run_full_pipeline(events, current_state=None, now=NOW, config=config)
    assert state.num_constituents == 10

    # Force full removal
    expired_markets = [
        _make_market(f"m{i}", end_date=PAST, volume_1mo=10000 - i) for i in range(15)
    ]
    expired_events = [
        _make_event(f"evt-m{i}", [m]) for i, m in enumerate(expired_markets)
    ]
    empty_state = run_full_pipeline(
        expired_events,
        current_state=state,
        now=NOW,
        config=config,
        reconstitute=False,
        rebalance=False,
    )
    assert empty_state.divisor == 0.0
    assert len(empty_state.constituents) == 0

    # Now reconstitute with 15 eligible markets → should get target_count=10, not buffer_top=8
    fresh_markets = [
        _make_market(f"new{i}", volume_1mo=10000 - i, end_date=FUTURE)
        for i in range(15)
    ]
    fresh_events = [
        _make_event(f"evt-new{i}", [m]) for i, m in enumerate(fresh_markets)
    ]
    recovered_state = run_full_pipeline(
        fresh_events,
        current_state=empty_state,
        now=NOW,
        config=config,
        reconstitute=True,
        rebalance=False,
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
        markets.append(
            _make_market(
                f"nr{i}",
                neg_risk=True,
                outcome_prices=prices,
                volume_1mo=1000.0 * (i + 1),
                volume_1wk=10.0,
                end_date=FUTURE,
            )
        )

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


def test_weight_cap_after_removal():
    """After expiration removal, no weight exceeds config.weight_cap."""
    # We need enough remaining constituents for the cap to be feasible.
    # With cap=0.05, need at least 20 constituents (1/0.05=20).
    # Build 25 constituents: top one has high volume, bottom 5 will expire.
    # After redistribution the top constituent would breach 5% without capping.
    config = IndexConfig(target_count=25, buffer_top=20, buffer_bottom=30,
                         weight_cap=0.05)
    n_total = 25
    n_expire = 5  # expire the bottom 5

    # Skewed volumes: m0 gets a huge share, rest are small
    volumes = [50000] + [1000] * (n_total - 1)
    markets = [
        _make_market(f"m{i}", volume_1mo=volumes[i], end_date=FUTURE)
        for i in range(n_total)
    ]
    events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(markets)]
    state = run_full_pipeline(events, current_state=None, now=NOW, config=config)
    assert state.num_constituents == n_total

    # Expire the bottom n_expire constituents
    mixed_markets = []
    for i in range(n_total):
        end = PAST if i >= n_total - n_expire else FUTURE
        mixed_markets.append(
            _make_market(f"m{i}", volume_1mo=volumes[i], end_date=end)
        )
    mixed_events = [
        _make_event(f"evt-m{i}", [m]) for i, m in enumerate(mixed_markets)
    ]

    new_state = run_full_pipeline(
        mixed_events,
        current_state=state,
        now=NOW,
        config=config,
        reconstitute=False,
        rebalance=False,
    )
    assert len(new_state.constituents) == n_total - n_expire
    for c in new_state.constituents:
        assert c.weight <= config.weight_cap + 1e-9, (
            f"Weight {c.weight:.6f} exceeds cap {config.weight_cap} for {c.id}"
        )
    print("  PASS: test_weight_cap_after_removal")


def test_cap_weights_few_constituents():
    """When n < 1/cap, cap_weights should assign equal weights."""
    from src.engine import cap_weights, Constituent
    # 10 constituents with 5% cap → infeasible (need 20), expect equal weights
    constituents = [
        Constituent(id=f"t{i}", label=f"t{i}", source_type="market",
                    num_outcomes=2, probabilities=[0.5, 0.5],
                    volume_1mo=1000, end_date=None,
                    weight=0.1)
        for i in range(10)
    ]
    result = cap_weights(constituents, cap=0.05)
    for c in result:
        assert abs(c.weight - 0.1) < 1e-12, f"Expected 0.1, got {c.weight}"
    assert abs(sum(c.weight for c in result) - 1.0) < 1e-12
    print("  PASS: test_cap_weights_few_constituents")


FUTURE_LONG = NOW + timedelta(days=365)


def _build_rebase_state(config=CFG):
    """Build an initial state with long-lived markets for rebase tests."""
    markets = [
        _make_market(f"m{i}", volume_1mo=10000 - i, end_date=FUTURE_LONG)
        for i in range(3)
    ]
    events = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(markets)]
    state = run_full_pipeline(events, current_state=None, now=NOW, config=config)
    return state, events


def test_rebase_on_first_run():
    """First run sets last_rebase to now and flags rebased=True."""
    state, _ = _build_rebase_state()
    assert state.rebased is True
    assert state.last_rebase == NOW
    assert state.value == 1000.0
    print("  PASS: test_rebase_on_first_run")


def test_rebase_does_not_trigger_before_interval():
    """Reconstitution before the 8-week boundary must not rebase."""
    state, _ = _build_rebase_state()
    initial_rebase_ts = state.last_rebase

    # 7 weeks later — just below the 8-week threshold
    now_later = NOW + timedelta(weeks=7)
    low_entropy_markets = [
        _make_market(
            f"m{i}",
            volume_1mo=10000 - i,
            end_date=FUTURE_LONG,
            outcome_prices=[0.9, 0.1],
        )
        for i in range(3)
    ]
    events_later = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(low_entropy_markets)]
    state2 = run_full_pipeline(
        events_later, current_state=state, now=now_later, reconstitute=True, config=CFG
    )
    assert state2.rebased is False
    assert state2.last_rebase == initial_rebase_ts
    assert state2.value < 1000.0  # entropy decay lowered the index
    print("  PASS: test_rebase_does_not_trigger_before_interval")


def test_rebase_triggers_at_interval():
    """Reconstitution at/after the 8-week boundary rebases index to 1000."""
    state, _ = _build_rebase_state()

    # 8 weeks + 1 day later — crosses the rebase boundary
    now_later = NOW + timedelta(weeks=8, days=1)
    low_entropy_markets = [
        _make_market(
            f"m{i}",
            volume_1mo=10000 - i,
            end_date=FUTURE_LONG,
            outcome_prices=[0.9, 0.1],
        )
        for i in range(3)
    ]
    events_later = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(low_entropy_markets)]
    state2 = run_full_pipeline(
        events_later, current_state=state, now=now_later, reconstitute=True, config=CFG
    )
    assert state2.rebased is True
    assert state2.last_rebase == now_later
    assert state2.value == 1000.0
    print("  PASS: test_rebase_triggers_at_interval")


def test_rebase_skips_on_rebalance_only():
    """Rebalance (without reconstitute) must not trigger rebase even past the interval."""
    state, _ = _build_rebase_state()
    initial_rebase_ts = state.last_rebase

    now_later = NOW + timedelta(weeks=10)
    low_entropy_markets = [
        _make_market(
            f"m{i}",
            volume_1mo=10000 - i,
            end_date=FUTURE_LONG,
            outcome_prices=[0.9, 0.1],
        )
        for i in range(3)
    ]
    events_later = [_make_event(f"evt-m{i}", [m]) for i, m in enumerate(low_entropy_markets)]
    state2 = run_full_pipeline(
        events_later,
        current_state=state,
        now=now_later,
        reconstitute=False,
        rebalance=True,
        config=CFG,
    )
    assert state2.rebased is False
    assert state2.last_rebase == initial_rebase_ts
    print("  PASS: test_rebase_skips_on_rebalance_only")


if __name__ == "__main__":
    print("Running engine edge-case tests...\n")
    test_full_removal_regular_update()
    test_full_removal_rebalance()
    test_empty_reconstitution()
    test_recovery_from_empty_selects_target_count()
    test_usable_markets_consistency()
    test_weight_cap_after_removal()
    test_cap_weights_few_constituents()
    test_rebase_on_first_run()
    test_rebase_does_not_trigger_before_interval()
    test_rebase_triggers_at_interval()
    test_rebase_skips_on_rebalance_only()
    print("\nAll tests passed.")
